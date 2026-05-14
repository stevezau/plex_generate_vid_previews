"""Registry of per-vendor :class:`VendorProcessor` implementations.

Each concrete vendor module (:mod:`.plex`, :mod:`.emby`, :mod:`.jellyfin`)
imports :func:`register_processor` and self-registers at module import
time. The orchestrator calls :func:`get_processor_for` with a
:class:`ServerType` and gets the right implementation back — no
caller branches on vendor type.

Importing :mod:`processing` (the package ``__init__``) imports the
three vendor modules, which is what triggers the registration. Direct
calls to :func:`get_processor_for` for an unknown vendor raise
``KeyError`` so silent fall-throughs aren't possible.
"""

from __future__ import annotations

from ..servers.base import ServerType
from .base import VendorProcessor

_PROCESSORS: dict[ServerType, VendorProcessor] = {}


def register_processor(server_type: ServerType, processor: VendorProcessor) -> None:
    """Register a :class:`VendorProcessor` for a given server type.

    Called at module import time from each vendor implementation.
    Re-registration is allowed (and silently overrides the prior entry)
    so test fixtures can swap in a fake without restart.
    """
    _PROCESSORS[server_type] = processor


def get_processor_for(server_type: ServerType | str) -> VendorProcessor:
    """Look up the :class:`VendorProcessor` for a server type.

    Args:
        server_type: Either a :class:`ServerType` enum member or the raw
            string value (``"plex"`` / ``"emby"`` / ``"jellyfin"``). The
            string form makes call sites that read straight from
            ``ServerConfig.type.value`` or settings JSON less verbose.

    Raises:
        KeyError: When no processor is registered for the requested vendor.
            This is intentionally loud — every supported vendor must register
            on import; a missing entry is a packaging bug, not a recoverable
            condition.
    """
    if isinstance(server_type, str):
        try:
            server_type = ServerType(server_type)
        except ValueError as exc:
            raise KeyError(f"unknown server type: {server_type!r}") from exc
    try:
        return _PROCESSORS[server_type]
    except KeyError as exc:
        raise KeyError(
            f"no VendorProcessor registered for {server_type} — "
            "ensure media_preview_generator.processing is imported (it "
            "triggers per-vendor registration on import)."
        ) from exc


def registered_types() -> list[ServerType]:
    """Return every server type with a registered processor.

    Primarily useful for tests + diagnostics ("which vendors are wired up").
    """
    return list(_PROCESSORS.keys())
