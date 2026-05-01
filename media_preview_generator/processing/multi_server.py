"""Multi-server processing entry point.

Path-centric orchestration that consumes the :class:`ServerRegistry` and
fans out a single FFmpeg pass to every server that owns the canonical
file. This is the *only* per-item entry point — webhooks, full-library
scans, and scheduled re-checks all dispatch through here.

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

import requests
from loguru import logger

from ..output import BifBundle, EmbyBifAdapter, JellyfinTrickplayAdapter, PlexBundleAdapter
from ..output.base import OutputAdapter
from ..output.journal import clear_meta, outputs_fresh_for_source, write_meta
from ..servers.base import LibraryNotYetIndexedError, MediaServer, ServerConfig, ServerType
from .frame_cache import get_frame_cache
from .generator import (
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

    PUBLISHED = "published"  # at least one publisher actually wrote new output
    SKIPPED = "skipped"  # owners exist but every one was skipped (output already on disk)
    SKIPPED_NOT_INDEXED = "skipped_not_indexed"  # owners exist but every one was waiting on the server's index
    NO_OWNERS = "no_owners"  # no enabled library covers the path
    FAILED = "failed"  # generation or every publisher failed
    NO_FRAMES = "no_frames"  # FFmpeg produced 0 frames (unrecoverable)


@dataclass
class PublisherResult:
    """Outcome of a single (server, adapter) publish attempt.

    ``frame_source`` records where the frames used by this publisher came
    from — independent of the publish ``status``. The Job UI surfaces this
    so users can tell whether one webhook was reused across multiple
    servers ("cache_hit") or whether the publisher's own output was
    already on disk ("output_existed") or whether FFmpeg actually ran
    just for this dispatch ("extracted").
    """

    server_id: str
    server_name: str
    adapter_name: str
    status: PublisherStatus
    output_paths: list[Path] = field(default_factory=list)
    message: str = ""
    frame_source: str = "extracted"  # one of: "extracted", "cache_hit", "output_existed"


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
                "Cannot publish Plex previews for server {!r}: its Plex config folder is not set. "
                "Open Settings → Media Servers, edit this server, and set the Plex config folder "
                "(the directory containing 'Media/localhost/...'). Other configured servers continue "
                "working normally.",
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
        "Server {!r} is configured for an unknown preview format ({!r}); skipping it. "
        "Edit this server in Settings → Media Servers and choose a supported format "
        "(plex_bundle, emby_sidecar, jellyfin_trickplay). Other servers continue working.",
        server_config.name,
        adapter_name,
    )
    return None


def _tmp_path_for(canonical_path: str, working_tmp_folder: str) -> str:
    """Return a deterministic per-file tmp directory.

    Hashing the canonical path keeps the directory short and unique
    even when path lengths exceed filesystem limits, and lets the
    frame cache key off the same hash.
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

    Servers that exclude this path via their per-server ``exclude_paths``
    rules are filtered out — letting users have different exclusion
    policies per server (e.g. skip a path on Jellyfin but still publish
    it on Plex).
    """
    from ..config import is_path_excluded

    publishers: list[tuple[MediaServer, OutputAdapter, str | None]] = []
    item_id_hints = item_id_by_server or {}

    matched_ids = {match.server_id for match in registry.find_owning_servers(canonical_path)}
    # Hints from the dispatcher are authoritative — if a Plex /tree call
    # already named the item, we trust the path lives on that server even
    # when the registry's library path-prefix matcher disagrees (common
    # when the test suite stubs Plex without populating library remote
    # paths, or when a user opens a one-off webhook with a path outside
    # the configured library roots).
    candidate_ids = list(matched_ids | set(item_id_hints.keys()))

    for sid in candidate_ids:
        server = registry.get(sid)
        if server is None:
            logger.debug(
                "Publisher candidate {} has no live client; skipping",
                sid,
            )
            continue

        cfg = registry.get_config(sid)
        if cfg is None:
            continue

        # Per-server exclude filter — same shape as the legacy global
        # exclude_paths setting, just scoped to one server.
        if cfg.exclude_paths and is_path_excluded(canonical_path, cfg.exclude_paths):
            logger.info(
                "Skipping {} on {!r} ({}) — matches that server's exclude_paths rules. "
                "Other configured servers may still publish this file.",
                canonical_path,
                cfg.name,
                cfg.id,
            )
            continue

        adapter = _adapter_for_server(cfg)
        if adapter is None:
            continue

        publishers.append((server, adapter, item_id_hints.get(sid)))

    return publishers


def _resolve_item_id_for(server: MediaServer, canonical_path: str, hint: str | None) -> str | None:
    """Get an item id for a server, preferring the dispatcher's hint.

    When the caller (webhook router, scan loop) already knows the
    server's item id we use it directly. Otherwise we ask the server
    via :meth:`MediaServer.resolve_remote_path_to_item_id`; servers
    without a reverse-lookup implementation return ``None`` (only
    Plex's bundle path and Jellyfin's manifest actually need the id,
    and the corresponding adapters degrade gracefully when missing).
    """
    if hint:
        return hint
    try:
        return server.resolve_remote_path_to_item_id(canonical_path)
    except Exception as exc:
        logger.debug(
            "resolve_remote_path_to_item_id failed for {} on {}: {}",
            canonical_path,
            server.name,
            exc,
        )
        return None


def _summarise_results(results: list[PublisherResult], status: MultiServerStatus) -> str:
    """Build a user-facing one-liner describing what happened across servers (D16).

    Replaces the old "N of M publisher(s) succeeded" wording, which (a)
    leaked the internal "publisher" term into UIs that talk about
    "servers" everywhere else, and (b) read as failure for skipped
    outcomes (a file already on disk would render as "0 of 1 succeeded"
    in the file's Details column, which users mistook for an error).
    """
    if not results:
        return ""
    n = len(results)
    word = "server" if n == 1 else "servers"
    if status is MultiServerStatus.PUBLISHED:
        published = sum(1 for r in results if r.status is PublisherStatus.PUBLISHED)
        if published == n:
            return f"Published to {n} {word}"
        return f"Published to {published} of {n} {word}"
    if status is MultiServerStatus.SKIPPED:
        return f"Already up to date on {n} {word}"
    if status is MultiServerStatus.SKIPPED_NOT_INDEXED:
        return f"Waiting for {n} {word} to finish indexing"
    if status is MultiServerStatus.FAILED:
        return f"Failed on {n} {word}"
    if status is MultiServerStatus.NO_FRAMES:
        return "FFmpeg produced no frames"
    if status is MultiServerStatus.NO_OWNERS:
        return "No server owns this path"
    return ""


def _publish_one(
    server: MediaServer,
    adapter: OutputAdapter,
    bundle: BifBundle,
    item_id: str | None,
    *,
    skip_if_exists: bool,
    frame_source: str = "extracted",
) -> PublisherResult:
    """Run one publisher; convert *expected* failures into a :class:`PublisherResult`.

    Only catches the runtime/IO/network/value exceptions adapters
    legitimately raise in their published contract. Programming errors
    (AttributeError, AssertionError, etc.) propagate so genuine bugs
    surface as test failures or 5xx responses instead of silently
    becoming a per-publisher ``FAILED`` row.

    ``frame_source`` records where the frames in ``bundle`` came from and
    is forwarded onto the result for UI display. The skip-if-exists path
    overrides this with ``"output_existed"`` because in that branch the
    publisher didn't need any frames at all.
    """
    try:
        output_paths = adapter.compute_output_paths(bundle, server, item_id)
    except LibraryNotYetIndexedError as exc:
        return PublisherResult(
            server_id=server.id,
            server_name=server.name,
            adapter_name=adapter.name,
            status=PublisherStatus.SKIPPED_NOT_INDEXED,
            message=str(exc),
            frame_source=frame_source,
        )
    except (TypeError, ValueError, OSError, RuntimeError, requests.RequestException) as exc:
        logger.warning(
            "Could not work out where to save previews for media server {!r}: {}. "
            "This usually means the server is unreachable or its API rejected our request — "
            "check the server's status, network connectivity, and credentials in Settings → Media Servers.",
            server.name,
            exc,
        )
        return PublisherResult(
            server_id=server.id,
            server_name=server.name,
            adapter_name=adapter.name,
            status=PublisherStatus.FAILED,
            message=f"Could not compute output paths: {exc}",
            frame_source=frame_source,
        )

    # Skip when every output exists AND the journal proves the source
    # hasn't changed since the last publish. The journal check guards
    # against the "Sonarr quality upgrade" case: file replaced in place,
    # outputs still on disk, but stale — mtime+size mismatch forces
    # regeneration. Falls through to publish if the meta is missing
    # (older publishes pre-journal) or if it doesn't match.
    if skip_if_exists and output_paths and outputs_fresh_for_source(output_paths, bundle.canonical_path):
        return PublisherResult(
            server_id=server.id,
            server_name=server.name,
            adapter_name=adapter.name,
            status=PublisherStatus.SKIPPED_OUTPUT_EXISTS,
            output_paths=output_paths,
            message="Output already exists (source unchanged)",
            frame_source="output_existed",
        )

    try:
        adapter.publish(bundle, output_paths, item_id)
    except (TypeError, ValueError, OSError, RuntimeError, requests.RequestException) as exc:
        logger.warning(
            "Failed to write preview output for media server {!r} (format: {}): {}. "
            "Common causes: write permission denied on the output folder, disk full, "
            "or the destination path doesn't exist. Verify the output folder is writable.",
            server.name,
            adapter.name,
            exc,
        )
        return PublisherResult(
            server_id=server.id,
            server_name=server.name,
            adapter_name=adapter.name,
            status=PublisherStatus.FAILED,
            output_paths=output_paths,
            message=f"Could not write preview file: {exc}",
            frame_source=frame_source,
        )

    # Stamp the journal so the next webhook for an unchanged source can
    # short-circuit. Best-effort — see ``write_meta``.
    write_meta(output_paths, bundle.canonical_path, publisher=adapter.name)

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
        frame_source=frame_source,
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
    regenerate: bool = False,
    use_frame_cache: bool = True,
    schedule_retry_on_not_indexed: bool = True,
    retry_attempt: int = 0,
    server_id_filter: str | None = None,
) -> MultiServerResult:
    """Process ``canonical_path`` and publish to every owning server.

    The single per-item entry point: consumes :class:`ServerRegistry`
    and the OutputAdapter pipeline. All callers — webhook router,
    full-library scans, scheduled re-checks, per-vendor processors —
    funnel through here.

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
    # Single line that ties every downstream log entry back to this dispatch.
    # On a server with N webhooks/min, this is the breadcrumb that lets ops
    # answer "what happened to this file?" without grep-searching for
    # disconnected log lines.
    logger.info(
        "Dispatch: path={} regenerate={} retry_attempt={}",
        canonical_path,
        regenerate,
        retry_attempt,
    )

    publishers = _resolve_publishers(
        canonical_path,
        registry,
        item_id_by_server=item_id_by_server,
    )
    if server_id_filter:
        # Job/webhook is pinned to a specific server — drop all other publishers
        # so we only publish previews for that server. Avoids the same-named-
        # library ambiguity when both Plex and Emby own the path.
        before = len(publishers)
        publishers = [p for p in publishers if p[0].id == server_id_filter]
        if before and not publishers:
            logger.info(
                "Dispatch pinned to server {!r} but that server doesn't own {} — skipping. "
                "(Other servers own this file but the job/webhook is scoped to one server only.)",
                server_id_filter,
                canonical_path,
            )
            return MultiServerResult(
                canonical_path=canonical_path,
                status=MultiServerStatus.NO_OWNERS,
                message=f"Pinned server {server_id_filter} does not own this path",
            )
    if not publishers:
        logger.info(
            "No owners: no configured server's enabled libraries cover {} — "
            "skipping (this is permanent until you add/enable a library)",
            canonical_path,
        )
        return MultiServerResult(
            canonical_path=canonical_path,
            status=MultiServerStatus.NO_OWNERS,
            message="No enabled library covers this path on any configured server",
        )

    logger.info(
        "Owners resolved: {} server(s) for {} → [{}]",
        len(publishers),
        canonical_path,
        ", ".join(f"{srv.name}/{adp.name}" for srv, adp, _ in publishers),
    )

    if not os.path.isfile(canonical_path):
        logger.warning(
            "Source video file is missing on disk: {}. "
            "This often happens when a webhook fires before the file finishes copying, or "
            "when the file was moved/deleted between scan and dispatch. "
            "If the file is supposed to be there, check your media mount and the path mapping "
            "under Settings → Media Servers. The rest of the queue is unaffected.",
            canonical_path,
        )
        return MultiServerResult(
            canonical_path=canonical_path,
            status=MultiServerStatus.FAILED,
            message=f"Source file not found: {canonical_path}",
        )

    # Pre-FFmpeg short-circuit: when every owning publisher's outputs
    # already exist AND the journal confirms the source hasn't changed
    # since the last publish, we can skip frame extraction entirely.
    # This handles the "Sonarr fires immediately, Plex's own webhook
    # follows 30 min later" scenario: the cache has expired but the
    # outputs are still on disk and still valid.
    #
    # When ``regenerate=True`` we deliberately bypass this and force a
    # fresh run; we also clear stale ``.meta`` sidecars so the new run
    # writes them rather than running into mismatched fingerprints
    # later. ``compute_output_paths`` may need to call the server (Plex
    # bundle hash); we tolerate failures here and fall back to the full
    # pipeline rather than spuriously refusing to publish.
    # ``compute_output_paths`` only needs the canonical_path and frame_interval
    # from a BifBundle; the frame_dir/bif_path/dimensions are placeholders for
    # the probe path. Build one helper so the three call-sites below stay in
    # sync (a divergence here previously hid behind copy-pasted dataclass kwargs).
    probe_frame_interval = int(getattr(config, "thumbnail_interval", 10) or 10)

    def _probe_bundle() -> BifBundle:
        return BifBundle(
            canonical_path=canonical_path,
            frame_dir=Path(os.devnull),  # unused by compute_output_paths
            bif_path=None,
            frame_interval=probe_frame_interval,
            width=320,
            height=180,
            frame_count=0,
        )

    if not regenerate:
        all_fresh = True
        for server, adapter, item_id_hint in publishers:
            try:
                item_id = _resolve_item_id_for(server, canonical_path, item_id_hint)
                paths = adapter.compute_output_paths(_probe_bundle(), server, item_id)
            except Exception:
                all_fresh = False
                break
            if not paths or not outputs_fresh_for_source(paths, canonical_path):
                all_fresh = False
                break
        if all_fresh:
            logger.info(
                "All publishers' outputs already fresh for {} — skipping FFmpeg",
                canonical_path,
            )
            results = []
            for server, adapter, item_id_hint in publishers:
                item_id = _resolve_item_id_for(server, canonical_path, item_id_hint)
                paths = adapter.compute_output_paths(_probe_bundle(), server, item_id)
                results.append(
                    PublisherResult(
                        server_id=server.id,
                        server_name=server.name,
                        adapter_name=adapter.name,
                        status=PublisherStatus.SKIPPED_OUTPUT_EXISTS,
                        output_paths=paths,
                        message="Output already exists (source unchanged)",
                        frame_source="output_existed",
                    )
                )
            return MultiServerResult(
                canonical_path=canonical_path,
                status=MultiServerStatus.SKIPPED,
                publishers=results,
                frame_count=0,
                message="All outputs fresh; FFmpeg skipped",
            )
    else:
        # Regenerate: drop stale ``.meta`` sidecars so a partial failure
        # mid-pipeline can't leave a fingerprint that misleads the next
        # short-circuit check. Best-effort.
        for server, adapter, item_id_hint in publishers:
            try:
                item_id = _resolve_item_id_for(server, canonical_path, item_id_hint)
                clear_meta(adapter.compute_output_paths(_probe_bundle(), server, item_id))
            except Exception:
                continue

    # Frame cache: when enabled, the second+ webhook for the same file
    # within the cache TTL skips FFmpeg entirely. Disabled callers
    # (regenerate=True, or callers that explicitly opt out) write into
    # an ad-hoc tmp dir that's cleaned up at the end.
    #
    # Anchor the cache at ``tmp_folder`` (stable across jobs), NOT at
    # ``working_tmp_folder`` (a per-job subdir created by job_runner).
    # The cache MUST outlive a single job — that's the whole point of
    # cross-job/cross-server reuse. Using the per-job dir produced
    # "FrameCache singleton already initialised with base_dir=..."
    # errors on the second job in a process.
    _cache_root = getattr(config, "tmp_folder", None) or getattr(config, "working_tmp_folder", "")
    # Don't let an empty _cache_root collapse to a relative path — would
    # materialise the cache in CWD (often `/` under gunicorn).
    if not _cache_root:
        import tempfile as _tempfile

        _cache_root = _tempfile.gettempdir()
    cache = get_frame_cache(base_dir=os.path.join(_cache_root, "frame_cache"))
    cache_hit: bool = False
    cleanup_path: str | None = None
    generation_lock = None

    # Acquire the per-path lock first (when caching is on) so the
    # subsequent cache.get / frame_dir_for / os.makedirs can't raise
    # without releasing it — every non-trivial step lives in the try
    # below whose finally always releases.
    if use_frame_cache and not regenerate:
        generation_lock = cache.generation_lock(canonical_path)
        generation_lock.acquire()

    try:
        if generation_lock is not None:
            # Per-path lock so simultaneous webhook fires for the same
            # canonical path serialise. The first thread generates; the
            # rest wait, then re-check the cache and hit it.
            cached = cache.get(canonical_path)
            if cached is not None:
                tmp_path = str(cached.frame_dir)
                frame_count = cached.frame_count
                cache_hit = True
                logger.info(
                    "Frames: REUSED from cache for {} ({} frames, no FFmpeg)",
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
            logger.info("Frames: EXTRACTING (cache miss) for {}", canonical_path)

        if not cache_hit:
            # K2: include server context. The dispatcher routes one canonical
            # path through one shared FFmpeg invocation that may serve multiple
            # publishers; the per-publisher follow-up logs already include
            # server.name (line ~685). Here we identify the source config view.
            _server_tag = getattr(config, "server_display_name", None) or "shared"
            logger.info(
                "FFmpeg start: server={} path={} gpu={} device={} tmp={}",
                _server_tag,
                canonical_path,
                gpu or "CPU",
                gpu_device_path or "-",
                tmp_path,
            )
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
                logger.info(
                    "Hardware acceleration could not handle the codec for {} — retrying on CPU automatically. "
                    "No action needed; this is a normal fallback for codecs your GPU doesn't support.",
                    canonical_path,
                )
                raise
            except Exception as exc:
                logger.exception(
                    "Could not extract preview frames from {} ({}: {}). "
                    "This file will be marked failed and skipped — the rest of the queue keeps running. "
                    "Common causes: corrupt video file, unsupported codec, or a crash inside FFmpeg's "
                    "hardware acceleration. The traceback above shows the exact failure; if it keeps "
                    "happening on the same file try toggling hardware acceleration off in Settings → GPU.",
                    canonical_path,
                    type(exc).__name__,
                    exc,
                )
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
            # Bake the diagnostic guidance into the message itself, not just
            # the log line. The Files panel's Details cell surfaces this
            # via record_file_result so the user can self-triage without
            # tailing the log.
            ms_message = (
                "FFmpeg produced 0 frames — file may be corrupt, codec not "
                "supported by FFmpeg, or video stream is empty. Try playing "
                "it in a media player to confirm it's intact."
            )
            logger.warning(
                "FFmpeg ran but produced no preview frames for {}. "
                "Most likely the file is corrupt, the codec is not supported by your FFmpeg build, "
                "or the video stream is zero-length. Try playing the file in a media player to "
                "confirm it's intact. Other files in the queue keep processing; this one is skipped.",
                canonical_path,
            )
            return MultiServerResult(
                canonical_path=canonical_path,
                status=MultiServerStatus.NO_FRAMES,
                message=ms_message,
            )

        # Store in cache only on a fresh generation; cache hits already
        # have an entry. Skip caching when use_frame_cache=False so
        # regenerate flows don't re-populate stale slots.
        if not cache_hit and use_frame_cache:
            cache.put(canonical_path, frame_dir=Path(tmp_path), frame_count=frame_count)

        # ``width``/``height`` are documentation-only on BifBundle —
        # adapters that need real frame dimensions (Jellyfin tile-grid)
        # measure them off the first JPG on disk. We surface what we
        # know from generate_images: it returns a tuple whose 4th
        # element is the requested width when the cache miss path ran;
        # cache-hit and non-tuple branches fall back to the default
        # 320x180 the FFmpeg pass uses.
        gen_width = int(gen_result[3]) if isinstance(gen_result, tuple) and len(gen_result) > 3 else 320
        bundle = BifBundle(
            canonical_path=canonical_path,
            frame_dir=Path(tmp_path),
            bif_path=None,
            frame_interval=int(getattr(config, "thumbnail_interval", 10) or 10),
            width=gen_width,
            height=180,
            frame_count=frame_count,
        )

        # Tag each publisher's result with where its frames came from so
        # the Job UI can render a distinct badge ("reused" vs "extracted").
        # The skip-if-exists branch inside _publish_one overrides this with
        # "output_existed" because in that case the publisher used no frames
        # at all.
        upstream_frame_source = "cache_hit" if cache_hit else "extracted"

        results: list[PublisherResult] = []
        for server, adapter, item_id_hint in publishers:
            item_id = _resolve_item_id_for(server, canonical_path, item_id_hint)
            outcome = _publish_one(
                server,
                adapter,
                bundle,
                item_id,
                skip_if_exists=not regenerate,
                frame_source=upstream_frame_source,
            )
            # One INFO line per publisher so an op debugging "which
            # server got the BIF and which didn't?" can scan the log
            # by canonical_path.
            log_fn = logger.info if outcome.status is PublisherStatus.PUBLISHED else logger.warning
            if outcome.status in (PublisherStatus.SKIPPED_OUTPUT_EXISTS, PublisherStatus.SKIPPED_NOT_INDEXED):
                log_fn = logger.info  # skipped is normal, not an error
            log_fn(
                "Publisher result: server={} adapter={} status={} item_id={} message={!r}",
                server.name,
                adapter.name,
                outcome.status.value,
                item_id or "-",
                outcome.message,
            )
            results.append(outcome)

        any_published = any(r.status is PublisherStatus.PUBLISHED for r in results)
        all_failed = all(r.status is PublisherStatus.FAILED for r in results)

        if any_published:
            status = MultiServerStatus.PUBLISHED
        elif all_failed:
            status = MultiServerStatus.FAILED
        elif results and all(r.status is PublisherStatus.SKIPPED_NOT_INDEXED for r in results):
            # D13 — distinct from SKIPPED so the file outcome and the
            # per-server chip render the same thing. Otherwise the row
            # shows "Already Existed" while the chip says "Not indexed",
            # confusing users into thinking the BIF is on disk when in
            # fact the server just hasn't scanned the file yet.
            status = MultiServerStatus.SKIPPED_NOT_INDEXED
        else:
            # No publisher actually wrote, but at least one wasn't a
            # hard failure — every publisher was skipped (output exists /
            # mixed not-indexed + output-exists). Reserve PUBLISHED for
            # "≥1 wrote" so callers don't conflate the two.
            status = MultiServerStatus.SKIPPED

        published_count = sum(1 for r in results if r.status is PublisherStatus.PUBLISHED)
        skipped_count = sum(
            1
            for r in results
            if r.status in (PublisherStatus.SKIPPED_OUTPUT_EXISTS, PublisherStatus.SKIPPED_NOT_INDEXED)
        )
        failed_count = sum(1 for r in results if r.status is PublisherStatus.FAILED)
        logger.info(
            "Dispatch complete: path={} status={} published={} skipped={} failed={} frames={}",
            canonical_path,
            status.value,
            published_count,
            skipped_count,
            failed_count,
            frame_count,
        )

        # Schedule a retry when at least one publisher is waiting for
        # the source server to finish indexing. Skipped via
        # ``schedule_retry_on_not_indexed=False`` from the retry
        # callback itself (it manages its own scheduling) and from
        # tests that want to assert the immediate result without
        # background timers spinning up.
        if (
            schedule_retry_on_not_indexed
            and status is not MultiServerStatus.FAILED
            and any(r.status is PublisherStatus.SKIPPED_NOT_INDEXED for r in results)
        ):
            from .retry_queue import schedule_retry_for_unindexed

            schedule_retry_for_unindexed(
                canonical_path,
                registry=registry,
                config=config,
                item_id_by_server=item_id_by_server,
                attempt=retry_attempt + 1,
            )

        return MultiServerResult(
            canonical_path=canonical_path,
            status=status,
            publishers=results,
            frame_count=frame_count,
            message=_summarise_results(results, status),
        )
    finally:
        # Only clean up tmp dirs that are *not* in the cache. Cache
        # entries persist for TTL so subsequent webhooks hit them.
        if cleanup_path is not None:
            _cleanup_temp_directory(cleanup_path)
        # Release the generation lock so waiting concurrent dispatchers
        # for the same canonical path can wake up and hit the now-
        # populated cache.
        if generation_lock is not None:
            generation_lock.release()
