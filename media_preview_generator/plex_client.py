"""Plex Media Server client and API interactions.

Handles Plex server connection, XML parsing monkey patch for debugging,
library querying, and duplicate location filtering.
"""

import http.client
import os
import time
import urllib.parse
import xml.etree.ElementTree
from dataclasses import dataclass, field

import requests
import urllib3
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import (
    Config,
    expand_path_mapping_candidates,
    is_path_excluded,
    local_path_to_webhook_aliases,
    path_to_canonical_local,
    plex_path_to_local,
)


def _log_prefix(config: Config) -> str:
    """Return the per-server log prefix for a Config view, or empty string.

    K2: when ``Config.server_display_name`` is set (i.e. the Config was
    derived from a specific media_servers entry), every Plex log line prepends
    ``[<name>] `` so multi-server installs get clear attribution. When unset
    (setup wizard / legacy global view), returns ``""`` and the wording stays
    backward-compatible.
    """
    name = getattr(config, "server_display_name", None)
    return f"[{name}] " if name else ""


def _plex_item_id(m) -> str:
    """Return Plex's bare ``ratingKey`` for ``m`` (e.g. ``"54321"``).

    Why: PlexAPI's ``m.key`` is the URL ``/library/metadata/<id>`` — passing it
    downstream as ``item_id`` doubles the prefix when ``PlexBundleAdapter`` builds
    ``/library/metadata/{item_id}/tree``, which silently 404s and reports
    ``skipped_not_indexed`` for every item. See D31.
    """
    raw = getattr(m, "ratingKey", None)
    if isinstance(raw, str | int) and str(raw).strip():
        return str(raw)
    key = str(getattr(m, "key", "") or "")
    return key.rsplit("/", 1)[-1] if key else key


def retry_plex_call(func, *args, max_retries=3, retry_delay=1.0, **kwargs):
    """Retry a Plex API call if it fails due to XML parsing errors.

    This handles cases where Plex returns incomplete XML due to being busy.

    Args:
        func: Function to call
        *args: Positional arguments for the function
        max_retries: Maximum number of retries (default: 3)
        retry_delay: Delay between retries in seconds (default: 1.0)
        **kwargs: Keyword arguments for the function

    Returns:
        Result of the function call

    Raises:
        Exception: If all retries fail

    """
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except xml.etree.ElementTree.ParseError as e:
            last_exception = e
            if attempt < max_retries:
                logger.warning(
                    "Plex returned a malformed response (attempt {} of {}); will retry in {:.1f}s. "
                    "This usually means Plex is busy or restarting — no action needed unless every attempt fails.",
                    attempt + 1,
                    max_retries + 1,
                    retry_delay,
                )
                time.sleep(retry_delay)
                retry_delay *= 1.5  # Exponential backoff
            else:
                logger.error(
                    "Plex kept returning malformed responses after {} attempts — giving up on this call. "
                    "Plex is likely overloaded or in the middle of a restart. Wait a minute and retry, "
                    "or restart Plex Media Server if it keeps happening. Other queued work continues; only this call failed.",
                    max_retries + 1,
                )
        except (
            ConnectionError,
            TimeoutError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ReadTimeout,
        ) as e:
            last_exception = e
            if attempt < max_retries:
                logger.warning(
                    "Couldn't reach Plex (attempt {} of {}); will retry in {:.1f}s. Underlying cause: {}. "
                    "If retries succeed, no action needed; if they all fail, check that Plex is running "
                    "and the URL in Settings → Plex is reachable.",
                    attempt + 1,
                    max_retries + 1,
                    retry_delay,
                    e,
                )
                time.sleep(retry_delay)
                retry_delay *= 1.5
            else:
                logger.error(
                    "Could not reach Plex after {} attempts — giving up on this call. Last error: {}. "
                    "Check that Plex is running, the URL and token in Settings → Plex are correct, and that "
                    "nothing on the network (firewall, VPN) is blocking the connection. The current job will "
                    "abort but other jobs and the web UI keep running.",
                    max_retries + 1,
                    e,
                )
        except Exception:
            # For other errors, don't retry
            raise

    # If we get here, all retries failed
    raise last_exception


def plex_server(config: Config):
    """Create Plex server connection with retry strategy and XML debugging.

    Args:
        config: Configuration object

    Returns:
        PlexServer: Configured Plex server instance

    Raises:
        ConnectionError: If unable to connect to Plex server
        requests.exceptions.RequestException: If connection fails after retries

    """
    # Plex Interface with retry strategy
    retry_strategy = Retry(
        total=3,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session = requests.Session()

    verify_ssl = config.plex_verify_ssl
    session.verify = verify_ssl
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        logger.warning(
            "SSL verification is DISABLED for Plex connections. Set PLEX_VERIFY_SSL=true (default) to re-enable."
        )
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # Create Plex server instance with proper error handling
    from plexapi.server import PlexServer

    try:
        logger.info("{}Connecting to Plex at {}...", _log_prefix(config), config.plex_url)
        plex = PlexServer(
            config.plex_url,
            config.plex_token,
            timeout=config.plex_timeout,
            session=session,
        )
        logger.info("{}Successfully connected to Plex", _log_prefix(config))
        return plex
    except (
        requests.exceptions.ConnectionError,
        requests.exceptions.ConnectTimeout,
        requests.exceptions.ReadTimeout,
        requests.exceptions.RequestException,
    ) as e:
        logger.error(
            "Could not connect to Plex at {}. Underlying cause: {}. "
            "Things to check: (1) Plex Media Server is running and reachable from this container/host; "
            "(2) the Plex URL in Settings → Plex is correct, including http:// or https:// and the port "
            "(usually 32400); (3) no firewall is blocking the connection. The current job will abort; the "
            "web UI and other servers continue working.",
            config.plex_url,
            e,
        )
        raise ConnectionError(f"Unable to connect to Plex server at {config.plex_url}: {e}") from e


def filter_duplicate_locations(media_items):
    """Filter out duplicate media items based on file locations.

    This function prevents processing the same video file multiple times
    when it appears in multiple episodes (common with multi-part episodes).
    It keeps the first occurrence and skips subsequent duplicates.

    Args:
        media_items: List of tuples (key, locations, title, media_type)

    Returns:
        list: Filtered list of tuples (key, title, media_type) without duplicates

    """
    seen_locations = set()
    filtered_items = []

    for key, locations, title, media_type in media_items:
        # Check if any location has been seen before
        if any(location in seen_locations for location in locations):
            continue

        # Add all locations to seen set and keep this item
        seen_locations.update(locations)
        filtered_items.append((key, title, media_type))  # Return tuple with key, title, and media_type

    return filtered_items


def _filter_excluded_by_path(media_items: list[tuple], config: Config) -> list[tuple]:
    """Drop items whose first location (mapped to local) is in config.exclude_paths."""
    exclude = getattr(config, "exclude_paths", None) or []
    if not exclude:
        return media_items
    mappings = getattr(config, "path_mappings", None) or []
    out = []
    for item in media_items:
        if len(item) == 4:
            key, locations, title, media_type = item
        else:
            out.append(item)
            continue
        locs = list(locations) if locations else []
        if not locs:
            out.append(item)
            continue
        local_path = path_to_canonical_local(locs[0], mappings) or locs[0]
        if is_path_excluded(local_path, exclude):
            continue
        out.append(item)
    return out


def get_library_sections(plex, config: Config, cancel_check=None, progress_callback=None):
    """Get all library sections from Plex server.

    Args:
        plex: Plex server instance
        config: Configuration object
        cancel_check: Optional callable returning True when processing should stop
        progress_callback: Optional callable(current, total, message) that
            surfaces pre-dispatch status to the UI while each library is
            being enumerated.

    Yields:
        tuple: (section, media_items) for each library

    """
    # Step 1: Get all library sections (1 API call)
    logger.info("Getting all Plex library sections...")
    if progress_callback:
        progress_callback(0, 0, "Listing Plex libraries...")
    start_time = time.time()

    try:
        sections = retry_plex_call(plex.library.sections)
    except (
        requests.exceptions.RequestException,
        http.client.BadStatusLine,
        xml.etree.ElementTree.ParseError,
    ) as e:
        logger.error(
            "Could not list Plex libraries after several retries — aborting this run. Underlying cause: {}. "
            "Most likely Plex is offline, the URL or token in Settings is wrong, or the network "
            "between this tool and Plex is broken. Confirm Plex is reachable at the configured "
            "URL, then click Test Connection in Settings → Plex.",
            e,
        )
        return

    sections_time = time.time() - start_time
    logger.info("Retrieved {} library sections in {:.2f} seconds", len(sections), sections_time)

    # Pre-filter sections so pre-dispatch progress reporting can show
    # "library i of N" using the count of libraries we'll actually scan,
    # not the raw section count returned by Plex.
    def _section_is_in_scope(section) -> bool:
        if getattr(config, "plex_library_ids", None):
            return str(section.key) in config.plex_library_ids
        if config.plex_libraries:
            return section.title.lower() in config.plex_libraries
        return True

    scoped_sections = [s for s in sections if _section_is_in_scope(s)]
    total_scoped = len(scoped_sections)

    # Step 2: Filter and process each library
    scoped_index = 0
    for section in sections:
        if cancel_check and cancel_check():
            logger.info("Cancellation detected during library scan — aborting")
            return
        # Filter by section key (ID) when plex_library_ids is set; otherwise by title (plex_libraries)
        if getattr(config, "plex_library_ids", None):
            if str(section.key) not in config.plex_library_ids:
                logger.info(
                    "Skipping library '{}' (id={}) as it's not in the configured library IDs list",
                    section.title,
                    section.key,
                )
                continue
        elif config.plex_libraries and section.title.lower() not in config.plex_libraries:
            logger.info("Skipping library '{}' as it's not in the configured libraries list", section.title)
            continue

        scoped_index += 1
        logger.info("Getting media files from library '{}'...", section.title)
        if progress_callback:
            progress_callback(
                0,
                0,
                f"Querying library '{section.title}' ({scoped_index}/{total_scoped}) — "
                "this can take a while for big libraries...",
            )
        library_start_time = time.time()

        # Determine sort parameter if sort_by is configured.
        # "random" is handled post-fetch by the orchestrator, so no Plex-side sort.
        sort_param = None
        if config.sort_by:
            if config.sort_by == "newest":
                sort_param = "addedAt:desc"
            elif config.sort_by == "oldest":
                sort_param = "addedAt:asc"

        try:
            if section.METADATA_TYPE == "episode":
                # Get episodes with locations for duplicate filtering
                search_kwargs = {"libtype": "episode"}
                if sort_param:
                    search_kwargs["sort"] = sort_param
                search_results = retry_plex_call(section.search, **search_kwargs)
                media_with_locations = []
                for m in search_results:
                    # Format episode title as "Show Title S01E01"
                    show_title = m.grandparentTitle
                    season_episode = m.seasonEpisode.upper()
                    formatted_title = f"{show_title} {season_episode}"
                    # D31 — store the bare ratingKey, NOT m.key. PlexAPI's m.key is the URL
                    # "/library/metadata/<id>"; passing that downstream as item_id causes
                    # PlexBundleAdapter to build "/library/metadata//library/metadata/<id>/tree"
                    # → 404, silently misreported as "not indexed yet".
                    media_with_locations.append((_plex_item_id(m), m.locations, formatted_title, "episode"))
                media_with_locations = _filter_excluded_by_path(media_with_locations, config)
                # Filter out multi episode files based on file locations
                media = filter_duplicate_locations(media_with_locations)
            elif section.METADATA_TYPE == "movie":
                search_kwargs = {}
                if sort_param:
                    search_kwargs["sort"] = sort_param
                search_results = retry_plex_call(section.search, **search_kwargs)
                media_with_locations = [
                    (_plex_item_id(m), getattr(m, "locations", []) or [], m.title, "movie") for m in search_results
                ]
                media_with_locations = _filter_excluded_by_path(media_with_locations, config)
                media = [(k, t, "movie") for k, _loc, t, _ in media_with_locations]
            else:
                logger.info("Skipping library {} as '{}' is unsupported", section.title, section.METADATA_TYPE)
                continue
        except (
            requests.exceptions.RequestException,
            http.client.BadStatusLine,
            xml.etree.ElementTree.ParseError,
        ) as e:
            logger.error(
                "Could not fetch items from Plex library {!r} after several retries — skipping it for this run. "
                "Underlying cause: {}. This usually means Plex is overloaded, restarting, or the library is huge "
                "and timing out. Try again in a few minutes; if it keeps failing, increase the Plex timeout under "
                "Settings → Plex. Other libraries are unaffected and will still be processed.",
                section.title,
                e,
            )
            continue

        library_time = time.time() - library_start_time
        logger.info(
            "Retrieved {} media files from library '{}' in {:.2f} seconds", len(media), section.title, library_time
        )

        if cancel_check and cancel_check():
            logger.info("Cancellation detected after retrieving library '{}' — aborting", section.title)
            return

        media_with_lib = [(k, t, mt, section.title) for k, t, mt in media]
        yield section, media_with_lib


def _normalize_path_for_match(path: str) -> str:
    """Normalize a path for case-insensitive cross-platform comparison."""
    normalized = os.path.normpath(path or "")
    return normalized.replace("\\", "/").lower()


def _map_plex_path_to_local(path: str, config: Config) -> str:
    """Map a Plex-reported path to local path using config.path_mappings."""
    mappings = getattr(config, "path_mappings", None) or []
    return plex_path_to_local(path, mappings) if mappings else path


def _build_episode_title(item) -> str:
    """Build display title for episode media items."""
    show_title = getattr(item, "grandparentTitle", "") or getattr(item, "title", "Unknown")
    season_episode = str(getattr(item, "seasonEpisode", "")).upper()
    return f"{show_title} {season_episode}".strip()


def _extract_item_locations(item) -> list[str]:
    """Extract non-empty locations from a Plex item."""
    locations = getattr(item, "locations", None) or []
    return [str(location).strip() for location in locations if str(location).strip()]


def _detect_path_prefix_mismatches(
    unresolved_paths: list[str],
    plex_locations: list[str],
) -> list[tuple[str, str]]:
    """Detect likely prefix mismatches between webhook paths and Plex library roots.

    For each unresolved path, checks whether any Plex library location appears
    as a path-boundary-aligned substring inside it.  When found, the differing
    prefixes are returned so callers can suggest the exact mapping the user needs.

    Args:
        unresolved_paths: Webhook paths that could not be resolved to Plex items.
        plex_locations: Root folder paths reported by Plex library sections.

    Returns:
        De-duplicated list of ``(webhook_prefix, plex_prefix)`` tuples, e.g.
        ``[("/data/media", "/media")]``.

    """
    if not unresolved_paths or not plex_locations:
        return []

    # Longest-first so more-specific locations match before broader ones.
    norm_locations = sorted(
        {loc.rstrip("/") for loc in plex_locations if loc.strip()},
        key=len,
        reverse=True,
    )

    seen: set[tuple[str, str]] = set()
    results: list[tuple[str, str]] = []

    for upath in unresolved_paths:
        upath_norm = upath.replace("\\", "/")
        upath_lower = upath_norm.lower()
        for plex_loc in norm_locations:
            plex_loc_lower = plex_loc.lower()

            idx = upath_lower.find(plex_loc_lower)
            if idx <= 0:
                continue

            # Ensure the Plex location isn't a partial segment match
            # (e.g. /media/tv must not match /media/tv2).
            end_idx = idx + len(plex_loc_lower)
            if end_idx < len(upath_lower) and upath_lower[end_idx] != "/":
                continue

            # Characters before the match are the extra webhook-only prefix.
            extra = upath_norm[:idx]

            # Suggest a mapping at the parent of the Plex location when
            # possible so it covers sibling libraries under the same root
            # (e.g. /media covers both /media/tv and /media/movies).
            plex_parent = os.path.dirname(plex_loc)
            if plex_parent and plex_parent != "/":
                plex_prefix = plex_parent
                webhook_prefix = extra.rstrip("/") + plex_parent
            else:
                plex_prefix = plex_loc
                webhook_prefix = extra.rstrip("/") + plex_loc

            pair = (webhook_prefix, plex_prefix)
            if pair not in seen:
                seen.add(pair)
                results.append(pair)
            break

    return results


def _mismatch_covered_by_mappings(
    webhook_pfx: str,
    plex_pfx: str,
    path_mappings: list[dict],
) -> bool:
    """Check whether a detected prefix mismatch is already handled by a configured mapping.

    A mismatch is considered covered when any mapping row maps between the
    Plex prefix (or its children) and the webhook prefix (via plex_prefix,
    local_prefix, or webhook_prefixes).

    Args:
        webhook_pfx: Webhook-side prefix detected by _detect_path_prefix_mismatches.
        plex_pfx: Plex-side prefix detected by _detect_path_prefix_mismatches.
        path_mappings: List from normalize_path_mappings().

    Returns:
        True if an existing mapping already covers this prefix pair.

    """
    if not path_mappings:
        return False
    wp = webhook_pfx.rstrip("/").lower()
    pp = plex_pfx.rstrip("/").lower()
    for row in path_mappings:
        # Accept either ``remote_prefix`` (the canonical multi-vendor key)
        # or the legacy ``plex_prefix`` alias so dedup correctly skips rows
        # written via the modern API.
        row_plex = (row.get("remote_prefix") or row.get("plex_prefix") or "").strip().rstrip("/").lower()
        row_local = (row.get("local_prefix") or "").strip().rstrip("/").lower()
        row_webhooks = [
            w.strip().rstrip("/").lower() for w in (row.get("webhook_prefixes") or []) if w and str(w).strip()
        ]
        plex_side = {row_plex, row_local}
        webhook_side = set(row_webhooks) | plex_side
        if pp in plex_side and wp in webhook_side:
            return True
    return False


def _resolve_item_media_type(section_type: str) -> str | None:
    """Map Plex section metadata type to internal media type."""
    if section_type == "movie":
        return "movie"
    if section_type == "episode":
        return "episode"
    return None


def trigger_plex_partial_scan(
    plex_url: str,
    plex_token: str,
    unresolved_paths: list[str],
    path_mappings: list[dict] | None = None,
    verify_ssl: bool = True,
    server_display_name: str | None = None,
) -> list[str]:
    """Trigger targeted Plex library scans for unresolved webhook paths.

    When webhook paths cannot be resolved to Plex items (because Plex hasn't
    scanned the file yet), this function determines the correct library section
    and parent folder for each unresolved path and issues a partial scan via
    ``GET /library/sections/{id}/refresh?path={folder}``.

    This is much faster than a full library scan and allows the subsequent
    retry attempt to find the item in Plex's database.

    Args:
        plex_url: Plex server URL (e.g. ``http://localhost:32400``).
        plex_token: Plex authentication token.
        unresolved_paths: File paths that could not be resolved to Plex items.
        path_mappings: Optional path mapping configuration for expanding
            webhook paths into Plex-native equivalents.
        verify_ssl: Whether to verify TLS certificates for Plex connections.

    Returns:
        List of paths for which a scan was successfully triggered.

    """
    if not unresolved_paths:
        return []

    # K2: per-server prefix for log lines below; empty when called without
    # a name (e.g. setup wizard).
    log_prefix = f"[{server_display_name}] " if server_display_name else ""

    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    scanned: list[str] = []

    try:
        resp = requests.get(
            f"{plex_url.rstrip('/')}/library/sections",
            headers={"X-Plex-Token": plex_token, "Accept": "application/json"},
            timeout=10,
            verify=verify_ssl,
        )
        resp.raise_for_status()
        sections = resp.json().get("MediaContainer", {}).get("Directory", [])
    except requests.RequestException as e:
        logger.warning(
            "Could not ask Plex for its library list to trigger a targeted scan: {}. "
            "Preview files were still saved — Plex just won't know about the new files until its own "
            "scheduled scan runs (or you trigger a scan manually in Plex). Verify Plex is reachable.",
            e,
        )
        return []

    # Build lookup: normalised location prefix -> (section_key, raw_location, section_title)
    # sorted longest-first so more-specific mounts match before broader ones.
    # K5: also capture the section title so we can render "section 2 (TV Shows)"
    # in scan-trigger logs — integer alone is opaque to humans.
    section_locations: list[tuple[str, str, str]] = []
    section_titles: dict[str, str] = {}
    for section in sections:
        section_key = str(section.get("key", ""))
        section_title = str(section.get("title", ""))
        if section_key and section_title:
            section_titles[section_key] = section_title
        for loc in section.get("Location", []):
            loc_path = loc.get("path", "")
            if loc_path:
                norm = loc_path.rstrip("/") + "/"
                section_locations.append((norm, section_key, loc_path))
    section_locations.sort(key=lambda t: len(t[0]), reverse=True)

    for unresolved in unresolved_paths:
        candidates = expand_path_mapping_candidates(unresolved, path_mappings) if path_mappings else [unresolved]
        scan_targets: set[tuple[str, str]] = set()

        for candidate in candidates:
            norm_candidate = candidate.rstrip("/") + "/"
            for loc_prefix, section_key, _raw_loc in section_locations:
                if not norm_candidate.startswith(loc_prefix):
                    continue

                # Use the top-level subfolder (series or movie folder).
                rel = candidate[len(loc_prefix.rstrip("/")) :]
                parts = [p for p in rel.split("/") if p]
                scan_folder = loc_prefix.rstrip("/") + "/" + parts[0] if len(parts) >= 2 else loc_prefix.rstrip("/")
                scan_targets.add((section_key, scan_folder))
                break  # First prefix match is best (sorted longest-first)

        if not scan_targets:
            logger.debug("No matching Plex library section found for unresolved path: {}", unresolved)
            continue

        triggered = False
        for section_key, scan_folder in sorted(scan_targets):
            try:
                scan_resp = requests.get(
                    f"{plex_url.rstrip('/')}/library/sections/{section_key}/refresh",
                    params={"path": scan_folder},
                    headers={"X-Plex-Token": plex_token},
                    timeout=10,
                    verify=verify_ssl,
                )
                if scan_resp.status_code == 200:
                    section_title = section_titles.get(section_key, "")
                    section_label = f'{section_key} ("{section_title}")' if section_title else section_key
                    logger.info(
                        "{}Triggered partial scan for section {}: {}",
                        log_prefix,
                        section_label,
                        scan_folder,
                    )
                    triggered = True
                else:
                    logger.warning(
                        "Plex refused to start a targeted scan (HTTP {}) for library section {} at {!r}. "
                        "Preview files were saved successfully — only the scan-trigger nudge failed; "
                        "Plex's next scheduled scan will pick the file up. If you see this often, "
                        "verify the Plex token in Settings → Plex still has admin permissions.",
                        scan_resp.status_code,
                        section_key,
                        scan_folder,
                    )
            except requests.RequestException as e:
                logger.warning(
                    "Could not ask Plex to scan the folder {!r}: {}. The preview file was still saved — "
                    "Plex just won't know about it until its own scheduled scan runs (or you trigger one manually).",
                    scan_folder,
                    e,
                )

        if triggered:
            scanned.append(unresolved)
        else:
            logger.debug("Matching Plex sections found but partial scans did not succeed for: {}", unresolved)

    if scanned:
        logger.info(
            "{}Triggered partial scans for {}/{} unresolved path(s)",
            log_prefix,
            len(scanned),
            len(unresolved_paths),
        )

    return scanned


VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov", ".flv", ".webm"})


def _expand_directory_to_media_files(
    paths: list[str],
    path_mappings: list[dict] | None = None,
) -> list[str]:
    """Expand directory paths into contained media files; pass file paths through unchanged.

    When a user submits a directory (e.g. a TV series root folder) via the
    manual trigger or webhook, this walks the directory tree and returns all
    video files found within it.  Non-directory paths are returned as-is so
    existing file-path behaviour is preserved.

    Path mappings are applied before the directory check so that a webhook-style
    path (e.g. ``/data/TV Shows/...``) can resolve to a local directory even
    when the local mount point differs (e.g. ``/data_16tb/TV Shows/...``).

    Args:
        paths: Absolute paths that may be files or directories.
        path_mappings: Optional path-mapping rows from config for resolving
            webhook/Plex paths to local equivalents.

    Returns:
        Flat list of file paths with directories replaced by their media files.
    """
    expanded: list[str] = []
    for path in paths:
        if not isinstance(path, str):
            expanded.append(path)
            continue

        candidates = expand_path_mapping_candidates(path, path_mappings) if path_mappings else [path]
        resolved_dir = None
        for candidate in candidates:
            if os.path.isdir(candidate):
                resolved_dir = candidate
                break

        if resolved_dir is not None:
            media_files = sorted(
                os.path.join(root, f)
                for root, _, files in os.walk(resolved_dir, followlinks=True)
                for f in files
                if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS
            )
            if media_files:
                if resolved_dir != path:
                    logger.info("Mapped directory '{}' -> '{}'", path, resolved_dir)
                logger.info("Expanded directory '{}' into {} media file(s)", resolved_dir, len(media_files))
                expanded.extend(media_files)
            else:
                logger.warning(
                    "Folder {!r} doesn't contain any recognised video files "
                    "(.mkv/.mp4/.avi/.m4v/.ts/.wmv/.mov/.flv/.webm). "
                    "Skipping this folder — the file path is being kept in the queue so other resolution "
                    "attempts can try it. If you expected videos here, check the file extensions and "
                    "folder permissions.",
                    resolved_dir,
                )
                expanded.append(path)
        else:
            expanded.append(path)
    return expanded


@dataclass
class WebhookResolutionResult:
    """Result of resolving webhook file paths to Plex media items."""

    items: list[tuple[str, str, str]]  # (key, title, media_type)
    unresolved_paths: list[str]
    skipped_paths: list[str]
    path_hints: list[str]
    # Pre-dedup matches with their Plex-side locations so the unified
    # process_canonical_path dispatcher can build ProcessableItems with
    # canonical_paths already known. Empty when the legacy 3-tuple path
    # is sufficient.
    items_with_locations: list[tuple[str, list[str], str, str]] = field(default_factory=list)


def get_media_items_by_paths(plex, config: Config, file_paths: list[str]) -> WebhookResolutionResult:
    """Resolve webhook file paths into Plex media tuples.

    We need Plex because preview BIF files must be written to a path derived from
    the item's bundle hash (from Plex). The webhook only gives us the file path;
    we look up the Plex item to get its key and then /tree to get the hash.

    We query Plex by file path (Plex API file= filter) so both new imports and
    file upgrades (where addedAt is unchanged) are found.

    Args:
        plex: Plex server instance.
        config: Runtime configuration (includes optional library filtering).
        file_paths: Absolute or mapped file paths from webhook payloads.

    Returns:
        WebhookResolutionResult with items (matched), unresolved_paths, and skipped_paths.

    """
    mappings = getattr(config, "path_mappings", None) or []
    file_paths = _expand_directory_to_media_files(file_paths or [], mappings)
    normalized_targets = set()
    input_paths: list[str] = []
    input_to_candidates: dict[str, list[str]] = {}
    input_to_targets: dict[str, set[str]] = {}
    basename_plex_locations: dict[str, list[str]] = {}
    for path in file_paths or []:
        if path is None:
            continue
        if not isinstance(path, str):
            logger.warning(
                "Webhook included a file path that wasn't text (got {} instead). "
                "Skipping it — check the webhook source's payload template if you keep seeing this.",
                type(path).__name__,
            )
            continue
        cleaned_path = path.strip()
        if not cleaned_path:
            continue
        # Expand into all equivalent mapped paths so webhook matching can fan out
        # across multi-disk roots until one matches.
        candidate_paths = expand_path_mapping_candidates(cleaned_path, mappings) if mappings else [cleaned_path]
        input_targets = {_normalize_path_for_match(candidate) for candidate in candidate_paths}
        if cleaned_path not in input_to_targets:
            input_paths.append(cleaned_path)
            input_to_targets[cleaned_path] = set()
            input_to_candidates[cleaned_path] = candidate_paths
        input_to_targets[cleaned_path].update(input_targets)
        normalized_targets.update(input_targets)
    if not normalized_targets:
        logger.info("Webhook path resolution skipped (no valid file paths provided)")
        return WebhookResolutionResult(items=[], unresolved_paths=[], skipped_paths=[], path_hints=[])

    num_input_files = len(input_paths)
    logger.info("Received {} webhook input file(s) to resolve", num_input_files)

    try:
        sections = retry_plex_call(plex.library.sections)
    except (
        requests.exceptions.RequestException,
        http.client.BadStatusLine,
        xml.etree.ElementTree.ParseError,
    ) as e:
        logger.error(
            "Could not list Plex libraries to resolve incoming webhook paths: {}. "
            "Webhooks for files this run can't be processed; verify Plex is reachable and the "
            "token is valid, then re-fire the webhook (or wait for the next scheduled scan).",
            e,
        )
        return WebhookResolutionResult(items=[], unresolved_paths=[], skipped_paths=[], path_hints=[])

    matched_items = []
    seen_keys = set()
    matched_targets = set()
    excluded_by_path_targets: set[str] = set()
    matched_target_to_plex_path: dict[str, str] = {}
    selected_match_libraries_by_target: dict[str, set[str]] = {}
    excluded_match_libraries_by_target: dict[str, set[str]] = {}
    selected_library_ids = {
        str(section_id).strip()
        for section_id in (getattr(config, "plex_library_ids", None) or [])
        if str(section_id).strip()
    }
    selected_library_titles = {
        str(name).strip().lower() for name in (getattr(config, "plex_libraries", None) or []) if str(name).strip()
    }

    def _is_selected_section(section) -> bool:
        """Return whether section is in the configured library scope."""
        if selected_library_ids:
            return str(getattr(section, "key", "")).strip() in selected_library_ids
        if selected_library_titles:
            return str(getattr(section, "title", "")).strip().lower() in selected_library_titles
        return True

    def _collect_item_targets(item) -> set[str]:
        """Build normalized path aliases used for webhook path matching."""
        item_targets = set()
        for location in _extract_item_locations(item):
            item_targets.add(_normalize_path_for_match(location))
            mapped_location = _map_plex_path_to_local(location, config)
            item_targets.add(_normalize_path_for_match(mapped_location))
            for alias in local_path_to_webhook_aliases(mapped_location, mappings):
                item_targets.add(_normalize_path_for_match(alias))
        return item_targets

    def _section_type_id(media_type: str | None) -> int | None:
        """Return Plex API type id for library section query (1=movie, 4=episode)."""
        if media_type == "movie":
            return 1
        if media_type == "episode":
            return 4
        return None

    def _search_by_file_path(target_paths: set[str]) -> set[str]:
        """Resolve paths by querying Plex with file= basename filter. Returns matched targets."""
        pass_matches = set()
        unresolved_inputs = [p for p in input_paths if not input_to_targets.get(p, set()).intersection(matched_targets)]
        basename_to_targets: dict[str, set[str]] = {}
        for p in unresolved_inputs:
            bn = os.path.basename(p)
            targets_for_path = input_to_targets.get(p, set()) & target_paths
            if targets_for_path:
                basename_to_targets.setdefault(bn, set()).update(targets_for_path)

        for section in sections:
            if not _is_selected_section(section):
                continue
            media_type = _resolve_item_media_type(getattr(section, "METADATA_TYPE", ""))
            if not media_type:
                continue
            type_id = _section_type_id(media_type)
            if type_id is None:
                continue
            section_key = getattr(section, "key", None)
            if section_key is None:
                continue
            section_title = str(getattr(section, "title", "Unknown")).strip() or "Unknown"
            for basename, targets_for_basename in basename_to_targets.items():
                if not targets_for_basename.intersection(target_paths - pass_matches):
                    continue
                try:
                    ekey = f"/library/sections/{section_key}/all?type={type_id}&file={urllib.parse.quote(basename)}"
                    items = retry_plex_call(plex.fetchItems, ekey)
                except (
                    requests.exceptions.RequestException,
                    http.client.BadStatusLine,
                    xml.etree.ElementTree.ParseError,
                ) as e:
                    logger.warning(
                        "Couldn't search Plex library {!r} for the webhook file (Plex error: {}). "
                        "This library will be skipped for this webhook only — other libraries are still being checked. "
                        "Re-fire the webhook in a minute or two if Plex was busy.",
                        section_title,
                        e,
                    )
                    continue
                if not items:
                    logger.debug(
                        "Plex file query returned 0 items for basename '{}' in library '{}'", basename, section_title
                    )
                for item in items:
                    item_targets = _collect_item_targets(item)
                    if not item_targets:
                        continue
                    matched_for_item = target_paths.intersection(item_targets)
                    if not matched_for_item:
                        plex_locs = _extract_item_locations(item)
                        logger.debug(
                            "Plex item '{}' matched basename but paths differ — Plex locations: {}",
                            getattr(item, "title", "?"),
                            plex_locs,
                        )
                        basename_plex_locations.setdefault(basename, []).extend(plex_locs)
                        continue
                    plex_locations = _extract_item_locations(item)
                    plex_path = plex_locations[0] if plex_locations else ""
                    # D31 — store the bare ratingKey, NOT item.key. PlexAPI's
                    # m.key is the URL "/library/metadata/<id>"; passing that
                    # downstream as item_id causes PlexBundleAdapter to build
                    # "/library/metadata//library/metadata/<id>/tree" → 404,
                    # silently misreported as "not indexed yet" for every
                    # Sonarr/Radarr → Plex webhook. We fall back to parsing
                    # the trailing segment of m.key when ratingKey isn't
                    # populated (older plexapi versions or odd response
                    # shapes); both paths yield the bare numeric id.
                    raw_rk = getattr(item, "ratingKey", None)
                    if isinstance(raw_rk, str | int) and str(raw_rk).strip():
                        item_key = str(raw_rk)
                    else:
                        url_key = str(getattr(item, "key", "") or "")
                        item_key = url_key.rsplit("/", 1)[-1] if url_key else ""
                    if not item_key:
                        logger.warning(
                            "Plex returned a search match for {} but didn't include the item's "
                            "metadata key — usually means Plex is still indexing this file. "
                            "It will be picked up on the next webhook or library scan.",
                            plex_path or "(unknown path)",
                        )
                        continue
                    if item_key in seen_keys:
                        continue
                    local_path = plex_path_to_local(plex_path, mappings) if plex_path else ""
                    if local_path and is_path_excluded(local_path, getattr(config, "exclude_paths", None)):
                        excluded_by_path_targets.update(matched_for_item)
                        continue
                    seen_keys.add(item_key)
                    pass_matches.update(matched_for_item)
                    for target in matched_for_item:
                        if target not in matched_target_to_plex_path and plex_path:
                            matched_target_to_plex_path[target] = plex_path
                        selected_match_libraries_by_target.setdefault(target, set()).add(section_title)
                    title = (
                        str(getattr(item, "title", "Unknown")).strip() or "Unknown"
                        if media_type == "movie"
                        else _build_episode_title(item)
                    )
                    matched_items.append((item_key, plex_locations or [], title, media_type))
        return pass_matches

    def _search_excluded_sections_by_file_path(target_paths: set[str]):
        """Return target paths that match Plex items in excluded libraries (file-path search)."""
        excluded_matches = set()
        excluded_sections = set()
        unresolved_inputs = [p for p in input_paths if not input_to_targets.get(p, set()).intersection(matched_targets)]
        basename_to_targets: dict[str, set[str]] = {}
        for p in unresolved_inputs:
            bn = os.path.basename(p)
            targets_for_path = input_to_targets.get(p, set()) & target_paths
            if targets_for_path:
                basename_to_targets.setdefault(bn, set()).update(targets_for_path)

        for section in sections:
            if _is_selected_section(section):
                continue
            media_type = _resolve_item_media_type(getattr(section, "METADATA_TYPE", ""))
            if not media_type:
                continue
            type_id = _section_type_id(media_type)
            if type_id is None:
                continue
            section_key = getattr(section, "key", None)
            if section_key is None:
                continue
            section_title = str(getattr(section, "title", "Unknown")).strip() or "Unknown"
            for basename, targets_for_basename in basename_to_targets.items():
                if not targets_for_basename.intersection(target_paths - excluded_matches):
                    continue
                try:
                    ekey = f"/library/sections/{section_key}/all?type={type_id}&file={urllib.parse.quote(basename)}"
                    items = retry_plex_call(plex.fetchItems, ekey)
                except (
                    requests.exceptions.RequestException,
                    http.client.BadStatusLine,
                    xml.etree.ElementTree.ParseError,
                ):
                    continue
                for item in items:
                    item_targets = _collect_item_targets(item)
                    if not item_targets:
                        continue
                    matched_for_item = target_paths.intersection(item_targets)
                    if matched_for_item:
                        excluded_matches.update(matched_for_item)
                        excluded_sections.add(section_title)
                        for target in matched_for_item:
                            excluded_match_libraries_by_target.setdefault(target, set()).add(section_title)
        return excluded_matches, excluded_sections

    logger.info("{}Querying Plex by file path...", _log_prefix(config))
    matched_targets.update(_search_by_file_path(normalized_targets))
    unresolved_targets = normalized_targets - matched_targets

    skipped_by_library_targets = set()
    skipped_library_names = set()
    if unresolved_targets and (selected_library_ids or selected_library_titles):
        skipped_by_library_targets, skipped_library_names = _search_excluded_sections_by_file_path(unresolved_targets)
        if skipped_by_library_targets:
            unresolved_targets = unresolved_targets - skipped_by_library_targets
            skipped_input_paths = [
                input_path
                for input_path in input_paths
                if input_to_targets.get(input_path, set()).intersection(skipped_by_library_targets)
            ]
            selected_scope = (
                ", ".join(sorted(selected_library_titles))
                if selected_library_titles
                else ", ".join(sorted(selected_library_ids))
            )
            if selected_scope:
                logger.info("Current selected library scope: {}", selected_scope)

    skipped_input_paths = [
        input_path
        for input_path in input_paths
        if input_to_targets.get(input_path, set()).intersection(skipped_by_library_targets)
    ]

    # Per-file outcome blocks (chronological, easy to follow).
    resolved_count = 0
    skipped_count = 0
    excluded_count = 0
    unresolved_count = 0
    resolved_input_paths_from_loop: set[str] = set()
    skipped_input_paths_from_loop: set[str] = set()
    excluded_input_paths_from_loop: set[str] = set()
    unresolved_input_paths_from_loop: list[str] = []
    max_detail_logs = 50
    total_for_index = len(input_paths)
    logger.info("Resolving {} webhook file(s):", total_for_index)
    for file_index, input_path in enumerate(input_paths[:max_detail_logs], start=1):
        input_targets = input_to_targets.get(input_path, set())
        candidate_paths = input_to_candidates.get(input_path, [input_path])
        mapped_candidates = [
            candidate
            for candidate in candidate_paths
            if _normalize_path_for_match(candidate) != _normalize_path_for_match(input_path)
        ]
        mapping_applied = bool(mapped_candidates)
        skipped_candidates = [
            candidate
            for candidate in candidate_paths
            if _normalize_path_for_match(candidate) in skipped_by_library_targets
        ]
        selected_libraries = sorted(
            {lib for target in input_targets for lib in selected_match_libraries_by_target.get(target, set())}
        )
        excluded_libraries = sorted(
            {lib for target in input_targets for lib in excluded_match_libraries_by_target.get(target, set())}
        )

        if input_targets.intersection(excluded_by_path_targets):
            excluded_count += 1
            excluded_input_paths_from_loop.add(input_path)
            logger.info("  [{}/{}] {}", file_index, total_for_index, input_path)
            logger.info("        Result: excluded (path rule — matches exclude list)")
            continue

        if input_targets.intersection(matched_targets):
            resolved_count += 1
            resolved_input_paths_from_loop.add(input_path)
            logger.info("  [{}/{}] {}", file_index, total_for_index, input_path)
            direct_match = _normalize_path_for_match(input_path) in matched_targets
            if not direct_match and mapping_applied:
                logger.info("        Direct path not found in Plex")
                mapping_prefixes = sorted(
                    set("/" + p.split("/")[1] for p in mapped_candidates if p.startswith("/") and len(p.split("/")) > 1)
                )
                logger.info("        {}Trying path mappings: {}", _log_prefix(config), ", ".join(mapping_prefixes))
                matched_norm = next(
                    iter(input_targets & matched_targets),
                    None,
                )
                plex_path = matched_target_to_plex_path.get(matched_norm) if matched_norm else None
                if plex_path:
                    logger.info("        Found via mapping: {}", plex_path)
            logger.info(
                "        Result: resolved"
                + (f" (library: {', '.join(selected_libraries)})" if selected_libraries else "")
            )
            continue

        if input_targets.intersection(skipped_by_library_targets):
            skipped_count += 1
            skipped_input_paths_from_loop.add(input_path)
            # Per-file diagnostic detail. Logged at info level — the
            # aggregate "Skipped N input file(s)" warning below is the
            # actionable summary; these lines just show the breakdown.
            logger.info("  [{}/{}] {}", file_index, total_for_index, input_path)
            if mapping_applied:
                mapping_prefixes = sorted(
                    set("/" + p.split("/")[1] for p in mapped_candidates if p.startswith("/") and len(p.split("/")) > 1)
                )
                logger.info("        {}Trying path mappings: {}", _log_prefix(config), ", ".join(mapping_prefixes))
            if skipped_candidates:
                logger.info("        Found in excluded library: {}", skipped_candidates[0])
            result_suffix = f": {', '.join(excluded_libraries)})" if excluded_libraries else ")"
            logger.info("        Result: skipped (excluded library" + result_suffix)
            continue

        unresolved_count += 1
        unresolved_input_paths_from_loop.append(input_path)
        # Per-file diagnostic detail. Logged at info level — the
        # aggregate "Unresolved" warning below is the actionable summary.
        logger.info("  [{}/{}] {}", file_index, total_for_index, input_path)
        logger.info("        Direct path not found in Plex")
        if mapping_applied:
            mapping_prefixes = sorted(
                set("/" + p.split("/")[1] for p in mapped_candidates if p.startswith("/") and len(p.split("/")) > 1)
            )
            logger.info("        {}Trying path mappings: {}", _log_prefix(config), ", ".join(mapping_prefixes))
        bn = os.path.basename(input_path)
        plex_locs = basename_plex_locations.get(bn)
        if plex_locs:
            logger.info("        {}Plex has file with same name at: {}", _log_prefix(config), plex_locs[0])
            if mapped_candidates:
                logger.info("        Webhook mapped to: {}", mapped_candidates[0])
            logger.info("        Result: not found (file exists in Plex but full paths differ)")
        else:
            logger.info("        Result: not found")

    # Classify any input paths beyond the per-file detail window (same formula as loop).
    for input_path in input_paths[max_detail_logs:]:
        targets = input_to_targets.get(input_path, set())
        if targets.intersection(excluded_by_path_targets):
            excluded_input_paths_from_loop.add(input_path)
            excluded_count += 1
        elif targets.intersection(matched_targets):
            resolved_input_paths_from_loop.add(input_path)
        elif targets.intersection(skipped_by_library_targets):
            skipped_input_paths_from_loop.add(input_path)
        else:
            unresolved_input_paths_from_loop.append(input_path)
    unresolved_input_paths = unresolved_input_paths_from_loop

    if len(input_paths) > max_detail_logs:
        logger.info("Webhook path detail truncated to first {} of {} file(s).", max_detail_logs, len(input_paths))

    # Summary (input-file counts first).
    summary_parts = [
        f"resolved={resolved_count}",
        f"skipped={skipped_count}",
        f"not found={unresolved_count}",
    ]
    if excluded_count > 0:
        summary_parts.insert(1, f"excluded={excluded_count} (path rule)")
    logger.info("Resolution summary: {} file(s) — {}", len(input_paths), ", ".join(summary_parts))
    if excluded_count > 0:
        logger.info("Excluded {} file(s) by path rule (see Settings → Exclude paths)", excluded_count)
    if skipped_input_paths:
        logger.warning(
            "Skipped {} file(s) from this webhook because they belong to Plex libraries you haven't "
            "selected for processing (found in: {}). Currently selected libraries: {}. "
            "If you want previews for these files too, open Settings → Plex and tick the additional libraries.",
            len(skipped_input_paths),
            ", ".join(sorted(skipped_library_names)),
            (
                ", ".join(sorted(selected_library_titles))
                if selected_library_titles
                else ", ".join(sorted(selected_library_ids))
            ),
        )
    path_hints: list[str] = []
    if unresolved_input_paths:
        cap = 10
        paths_to_log = unresolved_input_paths[:cap]
        if len(unresolved_input_paths) > cap:
            logger.warning(
                "Could not match {} of {} webhook file(s) to anything in Plex (showing the first {}): {}. "
                "Most often this means Plex hasn't indexed the file yet, or the path the webhook sent doesn't "
                "match how Plex sees the file. Check Settings → Path mappings and the hints below.",
                len(paths_to_log),
                len(unresolved_input_paths),
                cap,
                ", ".join(repr(p) for p in paths_to_log),
            )
        else:
            logger.warning(
                "Could not match these {} webhook file(s) to anything in Plex: {}. "
                "Most often this means Plex hasn't indexed the file yet, or the path the webhook sent doesn't "
                "match how Plex sees the file. Check Settings → Path mappings and the hints below.",
                len(paths_to_log),
                ", ".join(repr(p) for p in paths_to_log),
            )

        plex_roots = sorted(
            {
                str(loc).rstrip("/")
                for section in sections
                for loc in (getattr(section, "locations", None) or [])
                if str(loc).strip()
            }
        )
        if plex_roots:
            logger.info("{}Library paths: {}", _log_prefix(config), ", ".join(plex_roots))

        mismatches = _detect_path_prefix_mismatches(unresolved_input_paths, plex_roots)
        has_uncovered_mismatch = False
        for webhook_pfx, plex_pfx in mismatches:
            if _mismatch_covered_by_mappings(webhook_pfx, plex_pfx, mappings):
                hint = (
                    f"Path mapping '{webhook_pfx}' \u2192 '{plex_pfx}' is configured "
                    f"but file not found in Plex (may not be indexed yet)"
                )
            else:
                has_uncovered_mismatch = True
                hint = (
                    f"Possible prefix mismatch: webhook sends '{webhook_pfx}' "
                    f"but Plex uses '{plex_pfx}'. Consider adding a path mapping "
                    f"in Settings: Plex path = {plex_pfx}, "
                    f"Sonarr/Radarr path = {webhook_pfx}"
                )
            path_hints.append(hint)
            logger.info(hint)

        if has_uncovered_mismatch:
            logger.info(
                "If this app, Plex, or Sonarr/Radarr use different paths for the same files, "
                "configure Path mapping in Settings."
            )
    if excluded_count > 0 and len(matched_items) == 0:
        logger.info(
            "Matched {} path(s) in Plex; {} item(s) queued ({} excluded by path rule)",
            len(matched_targets) + len(excluded_by_path_targets),
            len(matched_items),
            excluded_count,
        )
    else:
        # Count webhook INPUTS that successfully resolved (not candidate
        # path-aliases). matched_targets is the post-aliasing set of
        # path candidates that matched in Plex, which can be 2-3× the
        # real input count when path mappings produce multiple aliases
        # per input. Reporting that as "N webhook path(s)" was
        # confusing — the user sends 1 file, sees "Resolved 2".
        resolved_inputs = sum(1 for p in input_paths if input_to_targets.get(p, set()).intersection(matched_targets))
        logger.info(
            "{}Resolved {} webhook input(s) into {} item(s)",
            _log_prefix(config),
            resolved_inputs,
            len(matched_items),
        )
    # Deduplicate by file location so multi-episode files are queued once (same as library scan).
    # Run the dedup once and emit both shapes: the legacy 3-tuple ``items``
    # callers still depend on, plus ``items_with_locations`` (4-tuple) so the
    # unified ProcessableItem dispatcher can build canonical_path-aware items
    # without making a second round-trip to Plex per webhook entry.
    matched_with_locations: list[tuple[str, list[str], str, str]] = []
    deduped_items: list[tuple[str, str, str]] = []
    seen_locations: set[str] = set()
    for key, locations, title, media_type in matched_items:
        if any(loc in seen_locations for loc in (locations or [])):
            continue
        if locations:
            seen_locations.update(locations)
        matched_with_locations.append((key, list(locations or []), title, media_type))
        deduped_items.append((key, title, media_type))
    return WebhookResolutionResult(
        items=deduped_items,
        unresolved_paths=unresolved_input_paths,
        skipped_paths=skipped_input_paths,
        path_hints=path_hints,
        items_with_locations=matched_with_locations,
    )
