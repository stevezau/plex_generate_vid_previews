"""Emby :class:`VendorProcessor` implementation.

Thin wrapper around :class:`_EmbyishProcessor` — Emby and Jellyfin
share their REST shape, so the heavy lifting lives in the shared
base. This module just identifies the vendor and self-registers
on import.
"""

from __future__ import annotations

from ..servers._embyish import EmbyApiClient
from ..servers.base import ServerConfig, ServerType
from ..servers.emby import EmbyServer
from ._embyish import _EmbyishProcessor
from .registry import register_processor


class EmbyProcessor(_EmbyishProcessor):
    vendor_name = "Emby"

    def _make_client(self, server_config: ServerConfig) -> EmbyApiClient:
        return EmbyServer(server_config)


register_processor(ServerType.EMBY, EmbyProcessor())
