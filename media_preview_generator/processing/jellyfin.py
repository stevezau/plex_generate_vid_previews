"""Jellyfin :class:`VendorProcessor` implementation.

Thin wrapper around :class:`_EmbyishProcessor` — Jellyfin shares its
REST shape with Emby, so the heavy lifting lives in the shared base.
This module just identifies the vendor and self-registers on import.
"""

from __future__ import annotations

from ..servers._embyish import EmbyApiClient
from ..servers.base import ServerConfig, ServerType
from ..servers.jellyfin import JellyfinServer
from ._embyish import _EmbyishProcessor
from .registry import register_processor


class JellyfinProcessor(_EmbyishProcessor):
    vendor_name = "Jellyfin"

    def _make_client(self, server_config: ServerConfig) -> EmbyApiClient:
        return JellyfinServer(server_config)


register_processor(ServerType.JELLYFIN, JellyfinProcessor())
