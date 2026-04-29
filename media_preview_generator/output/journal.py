"""Per-output ``.meta`` sidecar journal for source-aware skip-if-exists.

Background — what problem this solves:

The frame cache (process-wide, 10-min TTL) coalesces *concurrent* webhooks
for the same file. But when webhooks for the same file arrive *minutes
apart* (Sonarr fires immediately on import; Plex's own webhook fires
after its periodic library scan, often 30+ minutes later), the second
webhook arrives after the cache has expired. Today we re-extract frames
even though all publishers' outputs already exist on disk — wasted work.

Worse, if a user *replaces* the source file (Sonarr "upgrade" pulls a
higher-quality copy), the existing outputs are now stale. Plain
``output_paths.exists()`` skip-if-exists would happily reuse the old
BIF for a different source.

The fix is a ``.meta`` JSON sidecar written next to every published
output recording the source file's ``(mtime, size)`` at publish time.
Subsequent webhooks compare current source ``(mtime, size)`` against
the journal: match -> safely skip, mismatch -> force regenerate.

mtime + size is what Plex/Emby/Jellyfin themselves use for "changed"
detection; full hashes are overkill for this. The journal is portable
JSON (not xattrs), survives copies, and is readable by humans for
debugging.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from loguru import logger

#: Bumped whenever the on-disk schema changes. Older schemas are treated
#: as a miss (forces regen, then writes the new schema). Cheap to bump.
JOURNAL_SCHEMA_VERSION = 1


def _meta_path_for(output: Path) -> Path:
    """Return the ``.meta`` sidecar path for an output file.

    We append (not replace) the suffix so multiple outputs in the same
    directory don't collide and users can ``ls`` them next to the real
    files.
    """
    return output.with_suffix(output.suffix + ".meta")


def write_meta(output_paths: list[Path], canonical_path: str, *, publisher: str | None = None) -> None:
    """Stamp every output with the source file's freshness fingerprint.

    Called by ``_publish_one`` immediately after a successful publish.
    Writes one ``.meta`` per output so any single output that survives
    (e.g. user manually deleted the others) still carries the
    fingerprint and the dispatcher can make a correct freshness call.

    Failures here never bubble — at worst we miss a future short-circuit
    and re-run FFmpeg.
    """
    try:
        st = os.stat(canonical_path)
    except OSError:
        # Source vanished between publish and meta-write — leave the
        # outputs un-stamped so a future webhook re-publishes if the
        # source comes back.
        return

    payload = {
        "schema": JOURNAL_SCHEMA_VERSION,
        "source_path": canonical_path,
        "source_mtime": int(st.st_mtime),
        "source_size": int(st.st_size),
        "publisher": publisher or "",
    }
    body = json.dumps(payload, separators=(",", ":"))

    for output in output_paths:
        try:
            _meta_path_for(output).write_text(body)
        except OSError as exc:
            # Don't let a write failure here mask a successful publish.
            logger.debug("Could not write journal meta for {}: {}", output, exc)


def outputs_fresh_for_source(output_paths: list[Path], canonical_path: str) -> bool:
    """Return True iff outputs exist and, where journals exist, the source matches.

    "Fresh" semantics:

    * Every entry in ``output_paths`` must exist on disk.
    * If **no** ``.meta`` sidecars exist at all (legacy outputs from
      before the journal feature shipped), assume fresh — preserves the
      pre-journal skip-if-exists behavior so upgrading the tool doesn't
      force a regeneration storm. The next successful publish stamps a
      ``.meta`` so subsequent calls go through the strict path.
    * If **any** ``.meta`` sidecar exists, at least one must record
      ``(mtime, size)`` matching the current source. Conversely, if a
      ``.meta`` records a *different* fingerprint, fresh is False — the
      source has been replaced (Sonarr quality upgrade, manual swap),
      so skip-if-exists should not fire.

    Schema mismatch and I/O errors on a particular ``.meta`` are
    ignored; they neither prove nor disprove freshness. The ``stat``
    of the source itself failing returns False — better to regenerate
    than gamble.
    """
    if not output_paths:
        return False

    if not all(p.exists() for p in output_paths):
        return False

    try:
        st = os.stat(canonical_path)
    except OSError:
        return False
    src_mtime = int(st.st_mtime)
    src_size = int(st.st_size)

    saw_match = False
    saw_mismatch = False
    for output in output_paths:
        meta_path = _meta_path_for(output)
        if not meta_path.exists():
            continue
        try:
            data = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        if int(data.get("schema", 0)) != JOURNAL_SCHEMA_VERSION:
            continue
        if int(data.get("source_mtime", -1)) == src_mtime and int(data.get("source_size", -1)) == src_size:
            saw_match = True
        else:
            saw_mismatch = True

    if saw_match:
        return True
    if saw_mismatch:
        return False
    # No usable meta on any output — legacy outputs from before the
    # journal feature; preserve the pre-journal skip-if-exists semantic
    # so upgrades don't force a regeneration storm.
    return True


def clear_meta(output_paths: list[Path]) -> None:
    """Remove ``.meta`` sidecars for the given outputs.

    Used by force-regenerate flows so a stale fingerprint can't shortcut
    a freshly-requested run. Best-effort; missing files are fine.
    """
    for output in output_paths:
        try:
            _meta_path_for(output).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug("Could not remove stale meta for {}: {}", output, exc)
