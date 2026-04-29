"""Library-ownership resolution for the multi-server dispatcher.

A canonical local file path arriving at the dispatcher must be matched against
every configured server to decide:

1. Which servers will publish the BIF/trickplay output for this file.
2. Which servers should be skipped permanently (no enabled library covers the
   path) — so the dispatcher does not retry those.

The functions in this module are pure (no I/O, no global state) so they can be
unit-tested without spinning up a server registry. The dispatcher composes
them with the live :class:`MediaServer` registry to produce a publisher list
per inbound event.

Server-side library folder paths are translated to local paths via the
server's ``path_mappings`` before comparison; this is what lets the same
on-disk file be owned by Plex (mounted at ``/media``), Emby (mounted at
``/em-media``) and Jellyfin (``/jf-media``) simultaneously.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from typing import Any

from .base import Library, ServerConfig


@dataclass(frozen=True)
class OwnershipMatch:
    """Result of a positive ownership check.

    Attributes:
        server_id: ``ServerConfig.id`` of the matching server.
        library_id: ``Library.id`` whose folder covers the path.
        library_name: Human-readable library name (for logs).
        local_prefix: The local-path prefix that matched, useful for logging
            "matched under /data/movies".
    """

    server_id: str
    library_id: str
    library_name: str
    local_prefix: str


def _normalize(path: str) -> str:
    """Return ``path`` with a trailing slash so prefix matches are folder-bounded.

    Without the trailing slash, ``/data/movies`` would falsely match
    ``/data/movies-archive/foo.mkv``.

    Also Unicode-NFC normalises so a Japanese folder like ``メディア``
    typed by the user (NFC) matches the same name read off an HFS+ source
    mount (NFD). NFC is a no-op for ASCII paths.
    """
    return unicodedata.normalize("NFC", path.rstrip("/")) + "/"


def apply_path_mappings(remote_path: str, mappings: list[dict[str, Any]]) -> list[str]:
    """Translate a server-side path to candidate local paths.

    A single ``remote_path`` can map to several local prefixes when the user
    has configured multi-disk mounts. We return every plausible local
    candidate — the caller then matches the canonical path against any of
    them.

    The mapping dict shape mirrors the existing ``path_mappings`` schema in
    ``settings.json``: each entry has ``remote_prefix`` and ``local_prefix``
    (or the legacy ``plex_prefix``/``local_prefix`` shape).
    """
    if not mappings:
        return [remote_path]

    candidates: list[str] = []
    norm = _normalize(remote_path)
    for entry in mappings:
        remote = entry.get("remote_prefix") or entry.get("plex_prefix") or ""
        local = entry.get("local_prefix") or ""
        if not remote or not local:
            continue
        norm_remote = _normalize(remote)
        if norm.startswith(norm_remote):
            tail = remote_path[len(remote.rstrip("/")) :]
            candidates.append(local.rstrip("/") + tail)
    if not candidates:
        candidates.append(remote_path)
    return candidates


def server_owns_path(
    canonical_path: str,
    server: ServerConfig,
) -> OwnershipMatch | None:
    """Decide whether a server should publish the BIF for ``canonical_path``.

    A server "owns" a path when:

    - the server is enabled, **and**
    - some enabled library has at least one ``remote_paths`` entry which,
      after applying the server's ``path_mappings``, is a folder-bounded
      prefix of ``canonical_path``.

    Returns the first matching :class:`OwnershipMatch` or ``None``. Servers
    can declare overlapping libraries (e.g. "Movies" + "4K Movies"); the
    first match wins because that's enough to know the server should
    publish.
    """
    if not server.enabled:
        return None

    # NFC-normalise the canonical path *before* splitting; the basename
    # may be the bit that differs (NFD vs NFC) when the parent dir is
    # ASCII but the filename has accented characters.
    canonical_path = unicodedata.normalize("NFC", canonical_path)
    norm_path = _normalize(os.path.dirname(canonical_path)) + os.path.basename(canonical_path)

    for library in _enabled_libraries(server):
        for remote_path in library.remote_paths:
            # An empty/whitespace remote path would normalise to "/" and
            # match every absolute file path; reject those explicitly.
            if not (remote_path or "").strip():
                continue
            for local_candidate in apply_path_mappings(remote_path, server.path_mappings):
                if not (local_candidate or "").strip():
                    continue
                local_prefix = _normalize(local_candidate)
                if norm_path.startswith(local_prefix):
                    return OwnershipMatch(
                        server_id=server.id,
                        library_id=library.id,
                        library_name=library.name,
                        local_prefix=local_candidate,
                    )
    return None


def find_owning_servers(
    canonical_path: str,
    servers: list[ServerConfig],
) -> list[OwnershipMatch]:
    """Return ownership matches across every server in ``servers``.

    Order in the returned list follows the order in ``servers``; the
    dispatcher relies on that to keep telemetry stable across runs.
    """
    matches: list[OwnershipMatch] = []
    for server in servers:
        match = server_owns_path(canonical_path, server)
        if match is not None:
            matches.append(match)
    return matches


def _enabled_libraries(server: ServerConfig) -> list[Library]:
    """Return libraries for which the user has opted in, preserving order."""
    return [lib for lib in server.libraries if lib.enabled]
