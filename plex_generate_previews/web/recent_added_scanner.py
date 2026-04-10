"""Periodic Plex "recently added" scanner.

Queries Plex on a schedule for items that were added to a library within
the configured lookback window, then submits each file path through the
existing webhook job pipeline.

Dispatched as a first-class schedule type — see
``scheduler.execute_scheduled_job`` for the dispatch branch, and the
schedule's ``config`` dict for ``job_type`` and ``lookback_hours``.

**Stateless by design.**  Items that already have valid BIF previews are
skipped downstream by ``process_item()``'s existence check, so we don't
need to track a per-library cursor.  Re-submitting the same items every
tick is cheap and avoids race conditions on first install, restart, and
clock skew.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from loguru import logger


_SUPPORTED_LIBTYPES: tuple[tuple[str, str], ...] = (
    ("movie", "movie"),
    ("show", "episode"),
)


def _section_is_selected(
    section,
    explicit_ids: set[str],
    global_titles: set[str],
    global_ids: set[str],
) -> bool:
    """Decide whether a Plex section should be included in this scan.

    If ``explicit_ids`` is non-empty, only sections whose key matches an
    ID in that set are scanned (per-schedule library filter).

    Otherwise we fall back to the global ``selected_libraries`` setting
    (``global_titles`` + ``global_ids``).  If *that* is also empty, all
    supported sections are scanned.
    """
    section_id = str(getattr(section, "key", "") or "")
    section_title = str(getattr(section, "title", "") or "").strip().lower()

    if explicit_ids:
        return section_id in explicit_ids

    if not global_titles and not global_ids:
        return True

    if section_id and section_id in global_ids:
        return True
    if section_title and section_title in global_titles:
        return True
    return False


def _parse_global_library_filter(settings) -> tuple[set[str], set[str]]:
    """Parse the global ``selected_libraries`` setting into title/id sets."""
    raw = settings.get("selected_libraries", [])
    titles: set[str] = set()
    ids: set[str] = set()
    if isinstance(raw, list):
        for entry in raw:
            text = str(entry or "").strip()
            if not text:
                continue
            if text.isdigit():
                ids.add(text)
            else:
                titles.add(text.lower())
    return titles, ids


def _walk_item_paths(item) -> Iterable[str]:
    """Yield all file paths attached to a Plex media item."""
    media_list = getattr(item, "media", None) or []
    for media in media_list:
        for part in getattr(media, "parts", None) or []:
            file_path = getattr(part, "file", None)
            if file_path:
                yield str(file_path)


def _item_has_all_bifs(plex, item, plex_config_folder: str) -> bool:
    """Return ``True`` when every media part for this item already has a BIF.

    Queries Plex's ``/<item_key>/tree`` endpoint (same query
    :func:`plex_generate_previews.media_processing.process_item` uses) to
    recover the bundle hash for each media part, then checks whether the
    corresponding ``index-sd.bif`` file exists on disk.

    A single missing part means the worker needs to run, so we return
    ``False`` and let the item through.  On any query error we also
    return ``False`` — err on the side of submitting, because the
    worker will do its own authoritative BIF check.
    """
    item_key = getattr(item, "key", None)
    if not item_key or not plex_config_folder:
        return False
    try:
        data = plex.query(f"{item_key}/tree")
    except Exception as exc:
        logger.debug(
            "Recently Added: BIF check query failed for {}: {}",
            item_key,
            exc,
        )
        return False

    try:
        media_parts = list(data.findall(".//MediaPart")) if data is not None else []
    except (AttributeError, TypeError):
        return False
    if not media_parts:
        return False
    for media_part in media_parts:
        bundle_hash = media_part.attrib.get("hash") or ""
        if not bundle_hash or len(bundle_hash) < 2:
            # Invalid hash — the worker will record this as a skip, but we
            # don't want to silently filter it out here.
            return False
        bif_path = os.path.join(
            plex_config_folder,
            "Media",
            "localhost",
            f"{bundle_hash[0]}/{bundle_hash[1:]}.bundle",
            "Contents",
            "Indexes",
            "index-sd.bif",
        )
        if not os.path.isfile(bif_path):
            return False
    return True


def _iter_window_items(section, libtype: str, cutoff: datetime):
    """Yield items from a Plex section added since ``cutoff``.

    Falls back to a full section search + Python-side filtering when the
    Plex filter syntax isn't supported (some plexapi/PMS combinations
    don't accept ``addedAt>>`` for all section types).
    """
    from ..plex_client import retry_plex_call

    results: list = []
    try:
        results = retry_plex_call(
            section.search,
            libtype=libtype,
            filters={"addedAt>>": cutoff},
        )
    except Exception as exc:
        logger.debug(
            "Recently Added: addedAt filter failed on '{}' ({}); "
            "falling back to client-side filter",
            getattr(section, "title", "?"),
            exc,
        )
        try:
            results = retry_plex_call(
                section.search,
                libtype=libtype,
                sort="addedAt:desc",
            )
        except Exception as exc2:
            logger.warning(
                "Recently Added: section search failed for '{}': {}",
                getattr(section, "title", "?"),
                exc2,
            )
            return

    cutoff_naive = cutoff.replace(tzinfo=None)
    for item in results or []:
        added_at = getattr(item, "addedAt", None)
        if isinstance(added_at, datetime):
            added_naive = added_at.replace(tzinfo=None) if added_at.tzinfo else added_at
            if added_naive < cutoff_naive:
                # We're scanning newest-first; once we drop below the
                # window we can stop walking the section.
                return
        yield item


def _format_item_title(item) -> str:
    """Build a friendly display title for an item, mirroring the existing job UI."""
    media_type = getattr(item, "type", "")
    if media_type == "episode":
        show = getattr(item, "grandparentTitle", "") or "Unknown"
        season_ep = getattr(item, "seasonEpisode", "")
        if season_ep:
            return f"{show} {str(season_ep).upper()}"
        return show
    return getattr(item, "title", None) or "Unknown"


def scan_recently_added(
    lookback_hours: float,
    library_ids: Optional[list[str]] = None,
    *,
    plex=None,
    settings=None,
) -> int:
    """Scan Plex for items added within ``lookback_hours`` and queue jobs.

    Args:
        lookback_hours: How far back to look, in hours.  Fractional
            values are allowed (e.g. ``0.25`` = 15 minutes).  Clamped
            to ``0.25`` (15 min) .. ``720`` (30 days).
        library_ids: Optional list of section keys (as strings) to
            restrict the scan to.  When provided and non-empty, only
            those sections are scanned — the global
            ``selected_libraries`` setting is ignored.  When empty or
            ``None``, the scan falls back to ``selected_libraries``.
        plex: Optional ``PlexServer`` instance.  When omitted the
            function builds one from current settings.  Tests inject a
            mock here.
        settings: Optional settings manager instance (defaults to the
            module singleton).

    Returns:
        Number of paths submitted to the webhook job pipeline.
    """
    from .settings_manager import get_settings_manager
    from .webhooks import _add_history_entry, _schedule_webhook_job

    if settings is None:
        settings = get_settings_manager()

    try:
        lookback = float(lookback_hours)
    except (TypeError, ValueError):
        lookback = 1.0
    lookback = max(0.25, min(720.0, lookback))
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=int(lookback * 3600))

    explicit_ids: set[str] = set()
    if library_ids:
        explicit_ids = {str(v).strip() for v in library_ids if str(v).strip()}

    if plex is None:
        plex = _build_plex_client(settings)
        if plex is None:
            return 0

    try:
        from ..plex_client import retry_plex_call

        sections = retry_plex_call(plex.library.sections)
    except Exception as exc:
        logger.warning("Recently Added: failed to enumerate Plex sections: {}", exc)
        return 0

    global_titles, global_ids = _parse_global_library_filter(settings)

    # Pre-filter settings: when the user has regenerate_thumbnails on
    # they want EVERYTHING re-processed, so we skip the BIF-exists
    # filter and let the worker handle it.
    plex_config_folder = str(settings.get("plex_config_folder") or "/plex")
    regenerate = bool(settings.get("regenerate_thumbnails", False))

    submitted = 0
    scanned_sections = 0
    skipped_already_processed = 0

    for section in sections or []:
        if not _section_is_selected(section, explicit_ids, global_titles, global_ids):
            continue

        section_type = getattr(section, "type", None) or getattr(section, "TYPE", None)
        libtype: Optional[str] = None
        for raw_type, lt in _SUPPORTED_LIBTYPES:
            if str(section_type) == raw_type:
                libtype = lt
                break
        if libtype is None:
            continue

        scanned_sections += 1
        for item in _iter_window_items(section, libtype, cutoff):
            if not regenerate and _item_has_all_bifs(plex, item, plex_config_folder):
                skipped_already_processed += 1
                continue
            title = _format_item_title(item)
            for path in _walk_item_paths(item):
                if _schedule_webhook_job("recently_added", title, path):
                    submitted += 1

    if submitted:
        logger.info(
            "Recently Added: submitted {} new path(s) across {} section(s), "
            "skipped {} already-processed item(s) (lookback {:.2g}h)",
            submitted,
            scanned_sections,
            skipped_already_processed,
            lookback,
        )
        _add_history_entry(
            "recently_added",
            "Scan",
            f"{submitted} new file(s)",
            "queued",
            path_count=submitted,
        )
    elif skipped_already_processed:
        logger.info(
            "Recently Added: {} item(s) in the last {:.2g}h already have "
            "previews — nothing to do ({} section(s) scanned)",
            skipped_already_processed,
            lookback,
            scanned_sections,
        )
    else:
        logger.debug(
            "Recently Added: no new items in last {:.2g}h ({} section(s) scanned)",
            lookback,
            scanned_sections,
        )

    return submitted


def _build_plex_client(settings):
    """Construct a PlexServer using current settings.

    Returns ``None`` (and logs a warning) when the connection cannot be
    established.  The scheduled-job dispatch and the manual "Scan now"
    button both use this fallback; tests should pass an explicit
    ``plex`` argument to :func:`scan_recently_added` instead.
    """
    try:
        from ..config import load_config
        from ..plex_client import plex_server
    except ImportError as exc:
        logger.warning("Recently Added: cannot import Plex client modules: {}", exc)
        return None

    try:
        config = load_config()
    except Exception as exc:
        logger.warning("Recently Added: failed to load config: {}", exc)
        return None

    try:
        return plex_server(config)
    except Exception as exc:
        logger.warning("Recently Added: failed to connect to Plex: {}", exc)
        return None
