"""Shared helpers for route modules.

Provides path validation, GPU cache, and utility functions used across
the routes package.
"""

import os
import threading

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from loguru import logger

# Safe root directories for user-provided paths. All user-supplied
# paths must resolve within these directories before any filesystem
# operations are performed. Override via environment variables.
PLEX_DATA_ROOT = os.path.realpath(os.environ.get("PLEX_DATA_ROOT", "/plex"))
MEDIA_ROOT = os.path.realpath(os.environ.get("MEDIA_ROOT", "/"))

# Rate limiter — only applied to specific endpoints (login, auth).
# Dashboard APIs are exempt since they poll frequently.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URL", "memory://"),
)

# GPU detection cache (GPUs don't change at runtime).
# Detection runs once on first access; call clear_gpu_cache() to force re-scan.
_gpu_cache: dict = {"result": None}
_gpu_cache_lock = threading.Lock()


def _param_to_bool(value, default: bool) -> bool:
    """Coerce a request parameter (query-string or JSON) to bool.

    Uses the same truthy set as config.py ``get_value`` for consistency.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def _is_within_base(base_path: str, candidate_path: str) -> bool:
    """Return True if candidate_path is inside (or equal to) base_path.

    Both paths are resolved via os.path.realpath before comparison.
    Uses a trailing-separator check to avoid prefix collisions
    (e.g. /plex2 should not match /plex).
    """
    base_real = os.path.realpath(base_path)
    candidate_real = os.path.realpath(candidate_path)
    if base_real == candidate_real:
        return True
    if base_real == os.sep:
        return True
    base_with_sep = base_real if base_real.endswith(os.sep) else base_real + os.sep
    return candidate_real.startswith(base_with_sep)


def _safe_resolve_within(user_path: str, allowed_root: str) -> str | None:
    """Resolve a user-provided path and verify it stays within *allowed_root*.

    Returns the canonical absolute path on success, or ``None`` when the
    path contains null bytes or escapes the allowed root directory.

    The implementation uses ``os.path.normpath`` followed by a
    ``str.startswith`` guard, which is the pattern recognised by CodeQL
    as path-traversal sanitisation (py/path-injection).
    """
    if "\x00" in user_path:
        return None

    normalized = os.path.normpath(user_path)
    resolved = os.path.realpath(normalized)
    root_real = os.path.realpath(allowed_root)

    if resolved == root_real:
        return resolved
    if root_real == os.sep:
        return resolved
    if not resolved.startswith(root_real + os.sep):
        return None

    return resolved


def _ensure_gpu_cache() -> None:
    """Run GPU detection once and cache the result. No-op if already cached."""
    with _gpu_cache_lock:
        if _gpu_cache["result"] is not None:
            return

    try:
        from ...gpu_detection import detect_all_gpus

        raw_gpus = detect_all_gpus()
        gpus = []
        for gpu_type, device, info in raw_gpus:
            entry = dict(info) if isinstance(info, dict) else {}
            entry.setdefault("type", gpu_type)
            entry.setdefault("device", device)
            entry.setdefault("name", gpu_type)
            gpus.append(entry)
        with _gpu_cache_lock:
            _gpu_cache["result"] = gpus
        logger.debug(f"GPU detection complete: {len(gpus)} GPU(s)")
    except Exception as e:
        logger.warning(f"GPU detection failed: {e}")
        with _gpu_cache_lock:
            _gpu_cache["result"] = []


def clear_gpu_cache() -> None:
    """Reset the GPU detection cache.

    Useful for tests and when the user explicitly requests a re-scan.
    """
    with _gpu_cache_lock:
        _gpu_cache["result"] = None
