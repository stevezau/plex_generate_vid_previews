"""
Plex Media Server client and API interactions.

Handles Plex server connection, XML parsing monkey patch for debugging,
library querying, and duplicate location filtering.
"""

import os
import time
import urllib.parse
import http.client
import xml.etree.ElementTree
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from loguru import logger

from .config import (
    Config,
    expand_path_mapping_candidates,
    local_path_to_webhook_aliases,
    plex_path_to_local,
)


def retry_plex_call(func, *args, max_retries=3, retry_delay=1.0, **kwargs):
    """
    Retry a Plex API call if it fails due to XML parsing errors.

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
                    f"XML parsing error on attempt {attempt + 1}/{max_retries + 1}: {e}"
                )
                logger.info(f"Retrying in {retry_delay} seconds... (Plex may be busy)")
                time.sleep(retry_delay)
                retry_delay *= 1.5  # Exponential backoff
            else:
                logger.error(
                    f"XML parsing failed after {max_retries + 1} attempts: {e}"
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
                    f"Network error on attempt {attempt + 1}/{max_retries + 1}: {e}"
                )
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                retry_delay *= 1.5
            else:
                logger.error(
                    f"Network error failed after {max_retries + 1} attempts: {e}"
                )
        except Exception:
            # For other errors, don't retry
            raise

    # If we get here, all retries failed
    raise last_exception


def plex_server(config: Config):
    """
    Create Plex server connection with retry strategy and XML debugging.

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

    # SSL verification: default True, opt out via PLEX_VERIFY_SSL=false
    verify_ssl = os.environ.get("PLEX_VERIFY_SSL", "true").lower() not in (
        "false",
        "0",
        "no",
    )
    session.verify = verify_ssl
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        logger.warning(
            "SSL verification is DISABLED for Plex connections. "
            "Set PLEX_VERIFY_SSL=true (default) to re-enable."
        )
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    # Create Plex server instance with proper error handling
    from plexapi.server import PlexServer

    try:
        logger.info(f"Connecting to Plex server at {config.plex_url}...")
        plex = PlexServer(
            config.plex_url,
            config.plex_token,
            timeout=config.plex_timeout,
            session=session,
        )
        logger.info("Successfully connected to Plex server")
        return plex
    except (
        requests.exceptions.ConnectionError,
        requests.exceptions.ConnectTimeout,
        requests.exceptions.ReadTimeout,
        requests.exceptions.RequestException,
    ) as e:
        logger.error(f"Failed to connect to Plex server at {config.plex_url}")
        logger.error(f"Connection error: {e}")
        logger.error("Please check:")
        logger.error("  - Plex server is running and accessible")
        logger.error("  - Plex URL is correct (including http:// or https://)")
        logger.error("  - Network connectivity to Plex server")
        logger.error("  - Firewall settings allow connections to port 32400")
        raise ConnectionError(
            f"Unable to connect to Plex server at {config.plex_url}: {e}"
        ) from e


def filter_duplicate_locations(media_items):
    """
    Filter out duplicate media items based on file locations.

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
        filtered_items.append(
            (key, title, media_type)
        )  # Return tuple with key, title, and media_type

    return filtered_items


def get_library_sections(plex, config: Config, cancel_check=None):
    """
    Get all library sections from Plex server.

    Args:
        plex: Plex server instance
        config: Configuration object
        cancel_check: Optional callable returning True when processing should stop

    Yields:
        tuple: (section, media_items) for each library
    """
    import time

    # Step 1: Get all library sections (1 API call)
    logger.info("Getting all Plex library sections...")
    start_time = time.time()

    try:
        sections = retry_plex_call(plex.library.sections)
    except (
        requests.exceptions.RequestException,
        http.client.BadStatusLine,
        xml.etree.ElementTree.ParseError,
    ) as e:
        logger.error(f"Failed to get Plex library sections after retries: {e}")
        logger.error(f"Exception type: {type(e).__name__}")
        logger.error(
            "Cannot proceed without library access. Please check your Plex server status."
        )
        return

    sections_time = time.time() - start_time
    logger.info(
        f"Retrieved {len(sections)} library sections in {sections_time:.2f} seconds"
    )

    # Step 2: Filter and process each library
    for section in sections:
        if cancel_check and cancel_check():
            logger.info("Cancellation detected during library scan — aborting")
            return
        # Filter by section key (ID) when plex_library_ids is set; otherwise by title (plex_libraries)
        if getattr(config, "plex_library_ids", None):
            if str(section.key) not in config.plex_library_ids:
                logger.info(
                    "Skipping library '{}' (id={}) as it's not in the configured library IDs list".format(
                        section.title, section.key
                    )
                )
                continue
        elif (
            config.plex_libraries and section.title.lower() not in config.plex_libraries
        ):
            logger.info(
                "Skipping library '{}' as it's not in the configured libraries list".format(
                    section.title
                )
            )
            continue

        logger.info("Getting media files from library '{}'...".format(section.title))
        library_start_time = time.time()

        # Determine sort parameter if sort_by is configured
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
                    media_with_locations.append(
                        (m.key, m.locations, formatted_title, "episode")
                    )
                # Filter out multi episode files based on file locations
                media = filter_duplicate_locations(media_with_locations)
            elif section.METADATA_TYPE == "movie":
                search_kwargs = {}
                if sort_param:
                    search_kwargs["sort"] = sort_param
                search_results = retry_plex_call(section.search, **search_kwargs)
                media = [(m.key, m.title, "movie") for m in search_results]
            else:
                logger.info(
                    "Skipping library {} as '{}' is unsupported".format(
                        section.title, section.METADATA_TYPE
                    )
                )
                continue
        except (
            requests.exceptions.RequestException,
            http.client.BadStatusLine,
            xml.etree.ElementTree.ParseError,
        ) as e:
            logger.error(
                f"Failed to search library '{section.title}' after retries: {e}"
            )
            logger.error(f"Exception type: {type(e).__name__}")
            logger.warning(f"Skipping library '{section.title}' due to error")
            continue

        library_time = time.time() - library_start_time
        logger.info(
            "Retrieved {} media files from library '{}' in {:.2f} seconds".format(
                len(media), section.title, library_time
            )
        )

        if cancel_check and cancel_check():
            logger.info(
                f"Cancellation detected after retrieving library '{section.title}' — aborting"
            )
            return

        yield section, media


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
    show_title = getattr(item, "grandparentTitle", "") or getattr(
        item, "title", "Unknown"
    )
    season_episode = str(getattr(item, "seasonEpisode", "")).upper()
    return f"{show_title} {season_episode}".strip()


def _extract_item_locations(item) -> List[str]:
    """Extract non-empty locations from a Plex item."""
    locations = getattr(item, "locations", None) or []
    return [str(location).strip() for location in locations if str(location).strip()]


def _resolve_item_media_type(section_type: str) -> Optional[str]:
    """Map Plex section metadata type to internal media type."""
    if section_type == "movie":
        return "movie"
    if section_type == "episode":
        return "episode"
    return None


def trigger_plex_partial_scan(
    plex_url: str,
    plex_token: str,
    unresolved_paths: List[str],
    path_mappings: Optional[List[Dict]] = None,
) -> List[str]:
    """Trigger targeted Plex library scans for unresolved webhook paths.

    When webhook paths cannot be resolved to Plex items (because Plex hasn't
    scanned the file yet), this function determines the correct library section
    and parent folder for each unresolved path and issues a partial scan via
    ``GET /library/sections/{id}/refresh?path={folder}``.

    This is much faster than a full library scan and allows the subsequent
    retry attempt to find the item in Plex's database.

    Works for both movies and TV shows by matching the unresolved file path
    against each library section's configured locations.

    Args:
        plex_url: Plex server URL (e.g. ``http://localhost:32400``).
        plex_token: Plex authentication token.
        unresolved_paths: File paths that could not be resolved to Plex items.
            These are in Plex-path form (as seen by the Plex server).
        path_mappings: Optional path mapping configuration. Used to expand
            unresolved paths into Plex-native equivalents when the webhook
            uses different mount points than Plex.

    Returns:
        List of paths for which a scan was successfully triggered.
    """
    if not unresolved_paths:
        return []

    scanned: List[str] = []

    # Fetch library sections to match paths against section locations
    try:
        resp = requests.get(
            f"{plex_url.rstrip('/')}/library/sections",
            headers={"X-Plex-Token": plex_token, "Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        sections = resp.json().get("MediaContainer", {}).get("Directory", [])
    except Exception as e:
        logger.warning(f"Could not fetch Plex library sections for partial scan: {e}")
        return []

    # Build a lookup: normalised location prefix -> (section_key, raw_location)
    section_locations: List[Tuple[str, str, str]] = []
    for section in sections:
        section_key = str(section.get("key", ""))
        for loc in section.get("Location", []):
            loc_path = loc.get("path", "")
            if loc_path:
                norm = loc_path.rstrip("/") + "/"
                section_locations.append((norm, section_key, loc_path))

    # Sort longest prefix first so more-specific mounts match before broader ones
    section_locations.sort(key=lambda t: len(t[0]), reverse=True)

    for unresolved in unresolved_paths:
        # Expand the unresolved path into all mapping candidates (e.g. webhook
        # prefix -> plex prefix) so we can match against Plex section locations.
        if path_mappings:
            candidates = expand_path_mapping_candidates(unresolved, path_mappings)
        else:
            candidates = [unresolved]

        triggered = False
        for candidate in candidates:
            norm_candidate = candidate.rstrip("/") + "/"
            for loc_prefix, section_key, _raw_loc in section_locations:
                if norm_candidate.startswith(loc_prefix):
                    # Determine the scan folder: use the top-level subfolder
                    # (series folder for TV, movie folder for movies).
                    rel = candidate[
                        len(loc_prefix.rstrip("/")) :
                    ]  # e.g. /Show Name/Season 01/file.mkv
                    parts = [p for p in rel.split("/") if p]
                    if len(parts) >= 2:
                        # Use the top-level subfolder (series or movie folder)
                        scan_folder = loc_prefix.rstrip("/") + "/" + parts[0]
                    else:
                        # File is directly in the library root -- scan the root
                        scan_folder = loc_prefix.rstrip("/")

                    scan_url = (
                        f"{plex_url.rstrip('/')}/library/sections/{section_key}/refresh"
                    )
                    try:
                        scan_resp = requests.get(
                            scan_url,
                            params={"path": scan_folder},
                            headers={"X-Plex-Token": plex_token},
                            timeout=10,
                        )
                        if scan_resp.status_code == 200:
                            logger.info(
                                f"Triggered Plex partial scan for section {section_key}: {scan_folder}"
                            )
                            scanned.append(unresolved)
                            triggered = True
                            break
                        else:
                            logger.warning(
                                f"Plex partial scan returned HTTP {scan_resp.status_code} "
                                f"for section {section_key}, path {scan_folder}"
                            )
                    except Exception as e:
                        logger.warning(
                            f"Failed to trigger Plex partial scan for {scan_folder}: {e}"
                        )
                    break  # Matched a section, don't try other candidates
            if triggered:
                break

        if not triggered:
            logger.debug(
                f"No matching Plex library section found for unresolved path: {unresolved}"
            )

    if scanned:
        logger.info(
            f"Triggered Plex partial scans for {len(scanned)}/{len(unresolved_paths)} unresolved path(s)"
        )

    return scanned


@dataclass
class WebhookResolutionResult:
    """Result of resolving webhook file paths to Plex media items."""

    items: List[Tuple[str, str, str]]  # (key, title, media_type)
    unresolved_paths: List[str]
    skipped_paths: List[str]


def get_media_items_by_paths(
    plex, config: Config, file_paths: List[str]
) -> WebhookResolutionResult:
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
    normalized_targets = set()
    input_paths: List[str] = []
    input_to_candidates: Dict[str, List[str]] = {}
    input_to_targets: Dict[str, Set[str]] = {}
    for path in file_paths or []:
        if path is None:
            continue
        if not isinstance(path, str):
            logger.warning(
                "Webhook path resolution received non-string path value; skipping invalid entry"
            )
            continue
        cleaned_path = path.strip()
        if not cleaned_path:
            continue
        # Expand into all equivalent mapped paths so webhook matching can fan out
        # across multi-disk roots until one matches.
        candidate_paths = (
            expand_path_mapping_candidates(cleaned_path, mappings)
            if mappings
            else [cleaned_path]
        )
        input_targets = {
            _normalize_path_for_match(candidate) for candidate in candidate_paths
        }
        if cleaned_path not in input_to_targets:
            input_paths.append(cleaned_path)
            input_to_targets[cleaned_path] = set()
            input_to_candidates[cleaned_path] = candidate_paths
        input_to_targets[cleaned_path].update(input_targets)
        normalized_targets.update(input_targets)
    if not normalized_targets:
        logger.info("Webhook path resolution skipped (no valid file paths provided)")
        return WebhookResolutionResult(items=[], unresolved_paths=[], skipped_paths=[])

    num_input_files = len(input_paths)
    logger.info(f"Received {num_input_files} webhook input file(s) to resolve")

    try:
        sections = retry_plex_call(plex.library.sections)
    except (
        requests.exceptions.RequestException,
        http.client.BadStatusLine,
        xml.etree.ElementTree.ParseError,
    ) as e:
        logger.error(f"Failed to get Plex library sections for webhook paths: {e}")
        return WebhookResolutionResult(items=[], unresolved_paths=[], skipped_paths=[])

    matched_items = []
    seen_keys = set()
    matched_targets = set()
    matched_target_to_plex_path: Dict[str, str] = {}
    selected_match_libraries_by_target: Dict[str, Set[str]] = {}
    excluded_match_libraries_by_target: Dict[str, Set[str]] = {}
    selected_library_ids = {
        str(section_id).strip()
        for section_id in (getattr(config, "plex_library_ids", None) or [])
        if str(section_id).strip()
    }
    selected_library_titles = {
        str(name).strip().lower()
        for name in (getattr(config, "plex_libraries", None) or [])
        if str(name).strip()
    }

    def _is_selected_section(section) -> bool:
        """Return whether section is in the configured library scope."""
        if selected_library_ids:
            return str(getattr(section, "key", "")).strip() in selected_library_ids
        if selected_library_titles:
            return (
                str(getattr(section, "title", "")).strip().lower()
                in selected_library_titles
            )
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

    def _section_type_id(media_type: Optional[str]) -> Optional[int]:
        """Return Plex API type id for library section query (1=movie, 4=episode)."""
        if media_type == "movie":
            return 1
        if media_type == "episode":
            return 4
        return None

    def _search_by_file_path(target_paths: set[str]) -> set[str]:
        """Resolve paths by querying Plex with file= basename filter. Returns matched targets."""
        pass_matches = set()
        unresolved_inputs = [
            p
            for p in input_paths
            if not input_to_targets.get(p, set()).intersection(matched_targets)
        ]
        basename_to_targets: Dict[str, Set[str]] = {}
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
            section_title = (
                str(getattr(section, "title", "Unknown")).strip() or "Unknown"
            )
            for basename, targets_for_basename in basename_to_targets.items():
                if not targets_for_basename.intersection(target_paths - pass_matches):
                    continue
                try:
                    ekey = (
                        f"/library/sections/{section_key}/all"
                        f"?type={type_id}&file={urllib.parse.quote(basename)}"
                    )
                    items = retry_plex_call(plex.fetchItems, ekey)
                except (
                    requests.exceptions.RequestException,
                    http.client.BadStatusLine,
                    xml.etree.ElementTree.ParseError,
                ) as e:
                    logger.warning(
                        f"Skipping library '{section_title}' file-path search: {e}"
                    )
                    continue
                for item in items:
                    item_targets = _collect_item_targets(item)
                    if not item_targets:
                        continue
                    matched_for_item = target_paths.intersection(item_targets)
                    if not matched_for_item:
                        continue
                    pass_matches.update(matched_for_item)
                    plex_locations = _extract_item_locations(item)
                    plex_path = plex_locations[0] if plex_locations else ""
                    for target in matched_for_item:
                        if target not in matched_target_to_plex_path and plex_path:
                            matched_target_to_plex_path[target] = plex_path
                        selected_match_libraries_by_target.setdefault(
                            target, set()
                        ).add(section_title)
                    item_key = getattr(item, "key", None)
                    if not item_key:
                        logger.warning(
                            "Skipping matched Plex item without metadata key during webhook path resolution"
                        )
                        continue
                    if item_key in seen_keys:
                        continue
                    seen_keys.add(item_key)
                    title = (
                        str(getattr(item, "title", "Unknown")).strip() or "Unknown"
                        if media_type == "movie"
                        else _build_episode_title(item)
                    )
                    matched_items.append((item_key, title, media_type))
        return pass_matches

    def _search_excluded_sections_by_file_path(target_paths: set[str]):
        """Return target paths that match Plex items in excluded libraries (file-path search)."""
        excluded_matches = set()
        excluded_sections = set()
        unresolved_inputs = [
            p
            for p in input_paths
            if not input_to_targets.get(p, set()).intersection(matched_targets)
        ]
        basename_to_targets: Dict[str, Set[str]] = {}
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
            section_title = (
                str(getattr(section, "title", "Unknown")).strip() or "Unknown"
            )
            for basename, targets_for_basename in basename_to_targets.items():
                if not targets_for_basename.intersection(
                    target_paths - excluded_matches
                ):
                    continue
                try:
                    ekey = (
                        f"/library/sections/{section_key}/all"
                        f"?type={type_id}&file={urllib.parse.quote(basename)}"
                    )
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
                            excluded_match_libraries_by_target.setdefault(
                                target, set()
                            ).add(section_title)
        return excluded_matches, excluded_sections

    logger.info("Querying Plex by file path...")
    matched_targets.update(_search_by_file_path(normalized_targets))
    unresolved_targets = normalized_targets - matched_targets

    skipped_by_library_targets = set()
    skipped_library_names = set()
    if unresolved_targets and (selected_library_ids or selected_library_titles):
        skipped_by_library_targets, skipped_library_names = (
            _search_excluded_sections_by_file_path(unresolved_targets)
        )
        if skipped_by_library_targets:
            unresolved_targets = unresolved_targets - skipped_by_library_targets
            skipped_input_paths = [
                input_path
                for input_path in input_paths
                if input_to_targets.get(input_path, set()).intersection(
                    skipped_by_library_targets
                )
            ]
            selected_scope = (
                ", ".join(sorted(selected_library_titles))
                if selected_library_titles
                else ", ".join(sorted(selected_library_ids))
            )
            if selected_scope:
                logger.info(f"Current selected library scope: {selected_scope}")

    skipped_input_paths = [
        input_path
        for input_path in input_paths
        if input_to_targets.get(input_path, set()).intersection(
            skipped_by_library_targets
        )
    ]

    # Per-file outcome blocks (chronological, easy to follow).
    resolved_count = 0
    skipped_count = 0
    unresolved_count = 0
    resolved_input_paths_from_loop: Set[str] = set()
    skipped_input_paths_from_loop: Set[str] = set()
    unresolved_input_paths_from_loop: List[str] = []
    max_detail_logs = 50
    total_for_index = len(input_paths)
    logger.info(f"Resolving {total_for_index} webhook file(s):")
    for file_index, input_path in enumerate(input_paths[:max_detail_logs], start=1):
        input_targets = input_to_targets.get(input_path, set())
        candidate_paths = input_to_candidates.get(input_path, [input_path])
        mapped_candidates = [
            candidate
            for candidate in candidate_paths
            if _normalize_path_for_match(candidate)
            != _normalize_path_for_match(input_path)
        ]
        mapping_applied = bool(mapped_candidates)
        skipped_candidates = [
            candidate
            for candidate in candidate_paths
            if _normalize_path_for_match(candidate) in skipped_by_library_targets
        ]
        selected_libraries = sorted(
            {
                lib
                for target in input_targets
                for lib in selected_match_libraries_by_target.get(target, set())
            }
        )
        excluded_libraries = sorted(
            {
                lib
                for target in input_targets
                for lib in excluded_match_libraries_by_target.get(target, set())
            }
        )

        if input_targets.intersection(matched_targets):
            resolved_count += 1
            resolved_input_paths_from_loop.add(input_path)
            logger.info(f"  [{file_index}/{total_for_index}] {input_path}")
            direct_match = _normalize_path_for_match(input_path) in matched_targets
            if not direct_match and mapping_applied:
                logger.info("        Direct path not found in Plex")
                mapping_prefixes = sorted(
                    set(
                        "/" + p.split("/")[1]
                        for p in mapped_candidates
                        if p.startswith("/") and len(p.split("/")) > 1
                    )
                )
                logger.info(
                    f"        Trying path mappings: {', '.join(mapping_prefixes)}"
                )
                matched_norm = next(
                    iter(input_targets & matched_targets),
                    None,
                )
                plex_path = (
                    matched_target_to_plex_path.get(matched_norm)
                    if matched_norm
                    else None
                )
                if plex_path:
                    logger.info(f"        Found via mapping: {plex_path}")
            logger.info(
                "        Result: resolved"
                + (
                    f" (library: {', '.join(selected_libraries)})"
                    if selected_libraries
                    else ""
                )
            )
            continue

        if input_targets.intersection(skipped_by_library_targets):
            skipped_count += 1
            skipped_input_paths_from_loop.add(input_path)
            logger.warning(f"  [{file_index}/{total_for_index}] {input_path}")
            if mapping_applied:
                mapping_prefixes = sorted(
                    set(
                        "/" + p.split("/")[1]
                        for p in mapped_candidates
                        if p.startswith("/") and len(p.split("/")) > 1
                    )
                )
                logger.warning(
                    f"        Trying path mappings: {', '.join(mapping_prefixes)}"
                )
                if skipped_candidates:
                    logger.warning(
                        f"        Found in excluded library: {skipped_candidates[0]}"
                    )
            result_suffix = (
                f": {', '.join(excluded_libraries)})" if excluded_libraries else ")"
            )
            logger.warning("        Result: skipped (excluded library" + result_suffix)
            continue

        unresolved_count += 1
        unresolved_input_paths_from_loop.append(input_path)
        logger.warning(f"  [{file_index}/{total_for_index}] {input_path}")
        logger.warning("        Direct path not found in Plex")
        if mapping_applied:
            mapping_prefixes = sorted(
                set(
                    "/" + p.split("/")[1]
                    for p in mapped_candidates
                    if p.startswith("/") and len(p.split("/")) > 1
                )
            )
            logger.warning(
                f"        Trying path mappings: {', '.join(mapping_prefixes)}"
            )
        logger.warning("        Result: not found")

    # Classify any input paths beyond the per-file detail window (same formula as loop).
    for input_path in input_paths[max_detail_logs:]:
        targets = input_to_targets.get(input_path, set())
        if targets.intersection(matched_targets):
            resolved_input_paths_from_loop.add(input_path)
        elif targets.intersection(skipped_by_library_targets):
            skipped_input_paths_from_loop.add(input_path)
        else:
            unresolved_input_paths_from_loop.append(input_path)
    unresolved_input_paths = unresolved_input_paths_from_loop

    if len(input_paths) > max_detail_logs:
        logger.info(
            f"Webhook path detail truncated to first {max_detail_logs} of {len(input_paths)} file(s)."
        )

    # Summary (input-file counts first).
    logger.info(
        f"Resolution summary: {len(input_paths)} file(s) — "
        f"resolved={resolved_count}, skipped={skipped_count}, not found={unresolved_count}"
    )
    if skipped_input_paths:
        logger.warning(
            f"Skipped {len(skipped_input_paths)} input file(s): matched Plex items in unselected "
            f"libraries ({', '.join(sorted(skipped_library_names))}). "
            f"Selected scope: "
            + (
                ", ".join(sorted(selected_library_titles))
                if selected_library_titles
                else ", ".join(sorted(selected_library_ids))
            )
        )
    if unresolved_input_paths:
        cap = 10
        paths_to_log = unresolved_input_paths[:cap]
        if len(unresolved_input_paths) > cap:
            logger.warning(
                f"  Unresolved (first {cap} of {len(unresolved_input_paths)}): "
                + ", ".join(repr(p) for p in paths_to_log)
            )
        else:
            logger.warning("  Unresolved: " + ", ".join(repr(p) for p in paths_to_log))
        logger.info(
            "If this app, Plex, or Sonarr/Radarr use different paths for the same files, "
            "configure Path mapping in Settings."
        )
    logger.info(
        f"Resolved {len(matched_targets)} webhook path(s) into {len(matched_items)} Plex item(s)"
    )
    return WebhookResolutionResult(
        items=matched_items,
        unresolved_paths=unresolved_input_paths,
        skipped_paths=skipped_input_paths,
    )
