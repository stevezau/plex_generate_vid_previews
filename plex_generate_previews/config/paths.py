"""Path-mapping and library-selector helpers.

The path-mapping layer translates between a file's path inside Plex
(``/media/movies/foo.mkv``) and its path on the local filesystem where
this tool can read it (``/data/movies/foo.mkv``). Different deployment
topologies (NAS-as-Plex, Docker bind mounts, mergefs unions) need
different mapping shapes — all of them normalise into the same
``[{plex_prefix, local_prefix, webhook_prefixes?}]`` list used by the
rest of the codebase.

Also holds ``split_library_selectors`` which sorts user-provided library
strings into IDs vs. titles.
"""

import os
import re
import unicodedata
from typing import Any

from loguru import logger


def get_path_mapping_pairs(plex_mapping: str, local_mapping: str) -> list[tuple[str, str]]:
    """Parse path mapping config into (plex_root, local_root) pairs.

    Supports: (1) single pair when both are single values; (2) mergefs: multiple
    Plex roots (semicolon-separated) with one local path — all map to that path;
    (3) same count both sides — pair by index.

    Args:
        plex_mapping: Plex path(s), semicolon-separated for multiple.
        local_mapping: Local path(s), semicolon-separated or single.

    Returns:
        List of (plex_root, local_root) tuples to try in order.

    """
    plex_list = [s.strip() for s in (plex_mapping or "").split(";") if s.strip()]
    local_list = [s.strip() for s in (local_mapping or "").split(";") if s.strip()]
    if not plex_list or not local_list:
        return []
    if len(local_list) == 1:
        return [(plex_root, local_list[0]) for plex_root in plex_list]
    if len(plex_list) == len(local_list):
        return list(zip(plex_list, local_list, strict=True))
    # Mismatched lengths: use first of each (backward compat)
    return [(plex_list[0], local_list[0])]


# -----------------------------------------------------------------------------
# Path mappings (plex_prefix, local_prefix, optional webhook_prefixes)
# -----------------------------------------------------------------------------


def _normalize_prefix(p: str) -> str:
    """Return path with consistent trailing slash for prefix matching.

    Also Unicode-NFC-normalises the path. Filesystems on Linux store
    bytes; macOS HFS+ canonicalises to NFD on write. A user on Linux
    typing a Japanese folder name into settings (NFC) wouldn't match
    an NFD-encoded path coming from an HFS+ source mount otherwise.
    NFC is a no-op for ASCII; the cost is negligible.
    """
    if not p:
        return p
    return unicodedata.normalize("NFC", p.replace("\\", "/").rstrip("/")) or "/"


def _legacy_settings_to_path_mappings(plex_mapping: str, local_mapping: str) -> list[dict[str, Any]]:
    """Convert legacy semicolon pair config into path_mappings list."""
    pairs = get_path_mapping_pairs(plex_mapping or "", local_mapping or "")
    return [
        {"plex_prefix": plex_root, "local_prefix": local_root, "webhook_prefixes": []}
        for plex_root, local_root in pairs
    ]


def normalize_path_mappings(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Build path_mappings list from settings (new format or legacy).

    New format: settings["path_mappings"] is a list of dicts with keys
    plex_prefix, local_prefix, and optionally webhook_prefixes (list of strings).
    Legacy: settings has plex_videos_path_mapping and plex_local_videos_path_mapping
    (semicolon-separated); converted to mapping rows with empty webhook_prefixes.

    Args:
        settings: Dict from settings.json or equivalent (e.g. ui_settings).

    Returns:
        List of mapping dicts: {"plex_prefix", "local_prefix", "webhook_prefixes"}.

    """
    raw = settings.get("path_mappings")
    if isinstance(raw, list) and len(raw) > 0:
        out = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            plex = (row.get("plex_prefix") or "").strip()
            local = (row.get("local_prefix") or "").strip()
            if not plex or not local:
                continue
            web = row.get("webhook_prefixes")
            if isinstance(web, list):
                web = [s.strip() for s in web if s and str(s).strip()]
            else:
                web = []
            out.append({"plex_prefix": plex, "local_prefix": local, "webhook_prefixes": web})
        if out:
            return out
    # Legacy
    plex_str = (settings.get("plex_videos_path_mapping") or "").strip()
    local_str = (settings.get("plex_local_videos_path_mapping") or "").strip()
    if plex_str and local_str:
        return _legacy_settings_to_path_mappings(plex_str, local_str)
    return []


def _path_matches_prefix(path: str, prefix: str) -> bool:
    """Return True if path equals prefix or has prefix as a path prefix (no partial segment).

    Both sides are Unicode-NFC normalised before comparison so paths
    differing only in Unicode normal form (HFS+ NFD vs Linux/typed NFC)
    still match.
    """
    norm = _normalize_prefix(prefix)
    if not norm:
        return False
    path = unicodedata.normalize("NFC", (path or "").strip().replace("\\", "/"))
    return path == norm or path.startswith(norm + "/")


def normalize_exclude_paths(
    raw: list[Any] | None,
) -> list[dict[str, str]]:
    """Normalize exclude_paths from settings into list of {value, type} dicts.

    Accepts list of dicts with value/type or list of strings (treated as path prefix).
    """
    if not raw or not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if isinstance(entry, dict):
            value = (entry.get("value") or "").strip()
            kind = (entry.get("type") or "path").strip().lower()
            if not value:
                continue
            if kind not in ("path", "regex"):
                kind = "path"
            out.append({"value": value, "type": kind})
        elif isinstance(entry, str):
            value = entry.strip()
            if value:
                out.append({"value": value, "type": "path"})
    return out


def is_path_excluded(
    local_path: str,
    exclude_paths: list[dict[str, str]] | None,
) -> bool:
    """Return True if local_path is excluded by any rule (path prefix or regex).

    Args:
        local_path: Resolved local file path (as this app sees it).
        exclude_paths: List of {"value": str, "type": "path"|"regex"} from normalize_exclude_paths.

    Returns:
        True if the path should be skipped for preview generation.

    """
    if not local_path or not exclude_paths:
        return False
    path = os.path.normpath((local_path or "").strip()).replace("\\", "/")
    if not path:
        return False
    for entry in exclude_paths:
        value = (entry.get("value") or "").strip()
        kind = (entry.get("type") or "path").strip().lower()
        if not value:
            continue
        if kind == "regex":
            try:
                if re.search(value, path):
                    return True
            except re.error:
                logger.warning(
                    "Skipping an invalid exclude-paths regex: {!r}. The other exclude rules are still active. "
                    "Open Settings → Exclude paths and either fix or delete this rule "
                    "(test regex syntax at regex101.com if unsure).",
                    value[:50],
                )
                continue
        else:
            prefix = os.path.normpath(value).replace("\\", "/").rstrip("/")
            if not prefix:
                continue
            if path == prefix or path.startswith(prefix + "/"):
                return True
    return False


def path_to_canonical_local(path: str, path_mappings: list[dict[str, Any]]) -> str:
    """Map any path (Plex, webhook, or local) to canonical local path.

    Uses the first matching mapping: plex_prefix or any webhook_prefix is
    replaced by local_prefix. If no mapping matches, the path is returned
    unchanged (treated as already local).

    Args:
        path: Absolute path as seen by Plex, webhook, or this app.
        path_mappings: List from normalize_path_mappings().

    Returns:
        Path in the form this app can use for file access / comparison.

    """
    if not path or not path_mappings:
        return path or ""
    path = (path or "").strip().replace("\\", "/")
    for m in path_mappings:
        plex_prefix = _normalize_prefix(m.get("plex_prefix") or "")
        local_prefix = _normalize_prefix(m.get("local_prefix") or "")
        if plex_prefix and _path_matches_prefix(path, plex_prefix):
            rest = path[len(plex_prefix) :].lstrip("/")
            return f"{local_prefix.rstrip('/')}/{rest}" if rest else (local_prefix or "/")
        for wp in m.get("webhook_prefixes") or []:
            wp = _normalize_prefix(wp)
            if wp and _path_matches_prefix(path, wp):
                rest = path[len(wp) :].lstrip("/")
                return f"{local_prefix.rstrip('/')}/{rest}" if rest else (local_prefix or "/")
    return path


def expand_path_mapping_candidates(path: str, path_mappings: list[dict[str, Any]]) -> list[str]:
    """Return equivalent path candidates across all configured mapping rows.

    This helper expands a single input path into every plausible equivalent path
    using each mapping row. It is used for webhook matching so paths like
    ``/data/...`` can be tested against all mapped Plex roots (for example
    ``/data_16tb...``, ``/data_16tb2...``), not just the first matching row.

    Args:
        path: Absolute path reported by webhook/Plex/app.
        path_mappings: List from normalize_path_mappings().

    Returns:
        Ordered unique list of candidate paths. The original input path is first.

    """
    if not path:
        return []

    cleaned_path = str(path).strip().replace("\\", "/")
    if not cleaned_path:
        return []
    if not path_mappings:
        return [cleaned_path]

    candidates = [cleaned_path]
    seen = {cleaned_path}

    def _add_mapped_candidate(source_prefix: str, target_prefix: str) -> None:
        source = _normalize_prefix(source_prefix)
        target = _normalize_prefix(target_prefix)
        if not source or not target:
            return
        if not _path_matches_prefix(cleaned_path, source):
            return
        rest = cleaned_path[len(source) :].lstrip("/")
        candidate = f"{target.rstrip('/')}/{rest}" if rest else (target or "/")
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    for mapping in path_mappings:
        plex_prefix = mapping.get("plex_prefix") or ""
        local_prefix = mapping.get("local_prefix") or ""
        webhook_prefixes = mapping.get("webhook_prefixes") or []

        # Bidirectional Plex/local expansion for all rows.
        _add_mapped_candidate(plex_prefix, local_prefix)
        _add_mapped_candidate(local_prefix, plex_prefix)

        # Webhook aliases should fan out into both local and Plex forms.
        for webhook_prefix in webhook_prefixes:
            _add_mapped_candidate(webhook_prefix, local_prefix)
            _add_mapped_candidate(webhook_prefix, plex_prefix)
            _add_mapped_candidate(local_prefix, webhook_prefix)
            _add_mapped_candidate(plex_prefix, webhook_prefix)

    return candidates


def plex_path_to_local(path: str, path_mappings: list[dict[str, Any]]) -> str:
    """Map a Plex-reported path to local path (for file access)."""
    return path_to_canonical_local(path, path_mappings)


def local_path_to_webhook_aliases(path: str, path_mappings: list[dict[str, Any]]) -> list[str]:
    """Return webhook-style paths that could refer to the same file as the given local path.

    Used when matching webhook payloads (e.g. /data/...) to Plex items whose
    location is a specific disk (e.g. /data_16tb1/...). For each mapping where
    path starts with local_prefix and webhook_prefixes is set, returns path with
    local_prefix replaced by that webhook prefix.

    Args:
        path: Local path (e.g. /data_16tb1/Movies/foo.mkv).
        path_mappings: List from normalize_path_mappings().

    Returns:
        List of paths in webhook form (e.g. [/data/Movies/foo.mkv]).

    """
    if not path or not path_mappings:
        return []
    path = (path or "").strip().replace("\\", "/")
    out = []
    for m in path_mappings:
        local_prefix = _normalize_prefix(m.get("local_prefix") or "")
        if not local_prefix or not _path_matches_prefix(path, local_prefix):
            continue
        for wp in m.get("webhook_prefixes") or []:
            wp = _normalize_prefix(wp)
            if not wp or wp == local_prefix:
                continue
            rest = path[len(local_prefix) :].lstrip("/")
            alias = f"{wp.rstrip('/')}/{rest}" if rest else (wp or "/")
            out.append(alias)
    return out


def _is_library_id_value(value: str) -> bool:
    """Return True when a library selector value looks like a Plex section ID."""
    return bool(value) and value.isdigit()


def split_library_selectors(values: Any) -> tuple[list[str], list[str]]:
    """Split mixed library selectors into section IDs and lowercased titles.

    Args:
        values: Sequence of selector values from settings/API payloads.

    Returns:
        Tuple of (`library_ids`, `library_titles`) with duplicates removed while
        preserving order.

    """
    if not isinstance(values, list):
        return [], []

    library_ids: list[str] = []
    library_titles: list[str] = []
    seen_ids = set()
    seen_titles = set()

    for raw_value in values:
        if raw_value is None:
            continue
        value = str(raw_value).strip()
        if not value:
            continue
        if _is_library_id_value(value):
            if value not in seen_ids:
                seen_ids.add(value)
                library_ids.append(value)
            continue
        title = value.lower()
        if title not in seen_titles:
            seen_titles.add(title)
            library_titles.append(title)

    return library_ids, library_titles
