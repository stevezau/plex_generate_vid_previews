"""Emby implementation of the :class:`MediaServer` interface.

Most of the API surface is shared with Jellyfin (Jellyfin forked from
Emby) and lives in :mod:`._embyish`. This module specialises that base
with the Emby-only bits: the :class:`ServerType` enum value, the
path-based ``/Library/Media/Updated`` refresh endpoint, and the
Plex-format-compatible webhook plugin payload shape.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from ._embyish import EmbyApiClient
from .base import HealthCheckIssue, ServerType, WebhookEvent


class EmbyServer(EmbyApiClient):
    """Wrap a single Emby Server in the :class:`MediaServer` interface."""

    vendor_name = "Emby"

    def __init__(self, config) -> None:
        super().__init__(config, default_name="Emby")

    @property
    def type(self) -> ServerType:
        return ServerType.EMBY

    def trigger_refresh(self, *, item_id: str | None, remote_path: str | None) -> None:
        """Notify Emby that a media path changed.

        Prefers ``POST /Library/Media/Updated`` (path-based; matches the
        path-centric dispatcher). Falls back to a per-item refresh when
        only an item id is available. Failures are best-effort —
        publishers already wrote the BIF; the scan trigger is only a
        nudge so Emby picks the change up promptly.
        """
        if remote_path:
            try:
                response = self._request(
                    "POST",
                    "/Library/Media/Updated",
                    json_body={"Updates": [{"Path": remote_path, "UpdateType": "Modified"}]},
                )
                response.raise_for_status()
                return
            except Exception as exc:
                logger.debug("Emby /Library/Media/Updated failed for {}: {}", remote_path, exc)

        if item_id:
            try:
                response = self._request("POST", f"/Items/{item_id}/Refresh")
                response.raise_for_status()
            except Exception as exc:
                logger.debug("Emby item refresh failed for {}: {}", item_id, exc)

    def set_vendor_extraction(
        self,
        *,
        scan_extraction: bool,
        library_ids: list[str] | None = None,
    ) -> dict[str, str]:
        """Toggle Emby's chapter-image / trickplay scan-time extraction.

        Mirrors the Jellyfin path-mapped equivalent. Emby uses the
        ``ExtractChapterImagesDuringLibraryScan`` flag (chapter images
        are Emby's preview-thumbnail mechanism prior to its modern
        Trickplay support; ``EnableTrickplayImageExtraction`` controls
        the newer pipeline on Emby 4.8+ — we set both to be safe so
        the disable works on older + newer Emby installs).

        ``library_ids=None`` means "every library".
        """
        try:
            response = self._request("GET", "/Library/VirtualFolders")
            response.raise_for_status()
            folders = response.json()
        except Exception as exc:
            return {"_global": f"failed to fetch libraries: {exc}"}

        if not isinstance(folders, list):
            return {"_global": "unexpected VirtualFolders response shape"}

        results: dict[str, str] = {}
        target = set(library_ids) if library_ids else None
        for raw in folders:
            if not isinstance(raw, dict):
                continue
            lib_id = str(raw.get("ItemId") or raw.get("Id") or raw.get("Name") or "")
            if target is not None and lib_id not in target:
                continue

            options = dict(raw.get("LibraryOptions") or {})
            options["ExtractChapterImagesDuringLibraryScan"] = bool(scan_extraction)
            options["ExtractTrickplayImagesDuringLibraryScan"] = bool(scan_extraction)

            try:
                update = self._request(
                    "POST",
                    "/Library/VirtualFolders/LibraryOptions",
                    json_body={"Id": lib_id, "LibraryOptions": options},
                )
                update.raise_for_status()
                results[lib_id] = "ok"
            except Exception as exc:
                logger.warning(
                    "Could not update Emby library {} extraction on server {!r}: {}",
                    lib_id,
                    self.name,
                    exc,
                )
                results[lib_id] = f"error: {exc}"
        return results

    # ------------------------------------------------------------------
    # Per-library settings health check
    # ------------------------------------------------------------------
    #
    # Mirrors the JellyfinServer pattern but with Emby's flag set:
    #
    # * EnableTrickplayImageExtraction — only present on Emby 4.8+.
    #   Older Emby uses chapter-image extraction instead, surfaced via
    #   the separate ExtractChapterImagesDuringLibraryScan flag below.
    # * ExtractTrickplayImagesDuringLibraryScan — Emby 4.8+ trickplay.
    # * ExtractChapterImagesDuringLibraryScan — older Emby preview
    #   pipeline. We turn it off because chapter images aren't what
    #   our publisher writes; if Emby keeps generating them on scans
    #   it's wasted CPU regardless of our trickplay output.
    # * EnableRealtimeMonitor — Emby's "watch the filesystem for new
    #   files" toggle. Default off; on means new Sonarr/Radarr files
    #   show up without waiting for a manual scan or our scan-nudge.

    _RECOMMENDED_SETTINGS: tuple[tuple[str, str, bool, str, str], ...] = (
        (
            "ExtractTrickplayImagesDuringLibraryScan",
            "Skip Emby's own trickplay generation",
            False,
            "recommended",
            "When this app owns trickplay, Emby's scan-time extraction is wasted CPU and "
            "produces duplicate output. Off = let this app do it; on = Emby also burns CPU.",
        ),
        (
            "ExtractChapterImagesDuringLibraryScan",
            "Skip Emby's chapter-image extraction",
            False,
            "recommended",
            "Older Emby's preview-thumbnail mechanism. We don't write chapter images, so "
            "leaving this on means Emby generates them every scan with no display impact "
            "from anything this app publishes.",
        ),
        (
            "EnableRealtimeMonitor",
            "Auto-detect new files (real-time monitoring)",
            True,
            "recommended",
            "Without this, new files added by Sonarr/Radarr only get noticed on Emby's "
            "next manual scan or a webhook nudge — the 'not in library yet' status hangs "
            "around longer than it needs to.",
        ),
    )

    def check_settings_health(self) -> list[HealthCheckIssue]:
        """Return a per-library audit of preview-relevant Emby settings.

        Walks ``/Library/VirtualFolders`` once and emits one
        :class:`HealthCheckIssue` per (library, mis-set flag) pair.
        Empty list means all libraries are configured correctly.
        Flags are documented in :data:`_RECOMMENDED_SETTINGS`.
        """
        try:
            response = self._request("GET", "/Library/VirtualFolders")
            response.raise_for_status()
            folders = response.json()
        except Exception as exc:
            logger.warning(
                "Could not load Emby library settings for health check on {!r}: {}. "
                "The health-check panel will report 'unknown' until the server is reachable again.",
                self.name,
                exc,
            )
            return []

        if not isinstance(folders, list):
            return []

        issues: list[HealthCheckIssue] = []
        for raw in folders:
            if not isinstance(raw, dict):
                continue
            lib_id = str(raw.get("ItemId") or raw.get("Id") or raw.get("Name") or "")
            lib_name = str(raw.get("Name") or "")
            options = raw.get("LibraryOptions") or {}
            for flag, label, recommended, severity, rationale in self._RECOMMENDED_SETTINGS:
                # ExtractTrickplayImagesDuringLibraryScan is Emby 4.8+
                # only — if the older flag is the only one present we
                # silently skip the modern one (no issue to surface).
                if flag == "ExtractTrickplayImagesDuringLibraryScan" and flag not in options:
                    continue
                current = bool(options.get(flag, False))
                if current == recommended:
                    continue
                issues.append(
                    HealthCheckIssue(
                        library_id=lib_id,
                        library_name=lib_name,
                        flag=flag,
                        label=label,
                        rationale=rationale,
                        current=current,
                        recommended=recommended,
                        severity=severity,
                        fixable=True,
                    )
                )
        return issues

    def apply_recommended_settings(self, flags: list[str] | None = None) -> dict[str, str]:
        """Flip mis-set Emby library flags to their recommended values.

        Same wholesale-replace pattern as the Jellyfin variant
        (``/Library/VirtualFolders/LibraryOptions`` is a full POST
        of the existing options block with just our targeted flags
        rewritten — fields we omit revert to their defaults).
        Returns dict keyed ``"<library_id>:<flag>"``.
        """
        try:
            response = self._request("GET", "/Library/VirtualFolders")
            response.raise_for_status()
            folders = response.json()
        except Exception as exc:
            return {"_global": f"failed to fetch libraries: {exc}"}

        if not isinstance(folders, list):
            return {"_global": "unexpected VirtualFolders response shape"}

        target_flags = set(flags) if flags is not None else None
        results: dict[str, str] = {}

        for raw in folders:
            if not isinstance(raw, dict):
                continue
            lib_id = str(raw.get("ItemId") or raw.get("Id") or raw.get("Name") or "")
            options = dict(raw.get("LibraryOptions") or {})

            changed_flags: list[str] = []
            for flag, _label, recommended, _sev, _rationale in self._RECOMMENDED_SETTINGS:
                if target_flags is not None and flag not in target_flags:
                    continue
                if flag == "ExtractTrickplayImagesDuringLibraryScan" and flag not in options:
                    continue
                if bool(options.get(flag, False)) == recommended:
                    continue
                options[flag] = recommended
                changed_flags.append(flag)

            if not changed_flags:
                continue

            try:
                update = self._request(
                    "POST",
                    "/Library/VirtualFolders/LibraryOptions",
                    json_body={"Id": lib_id, "LibraryOptions": options},
                )
                update.raise_for_status()
                for flag in changed_flags:
                    results[f"{lib_id}:{flag}"] = "ok"
            except Exception as exc:
                logger.warning(
                    "Could not update Emby library {} settings on server {!r}: {}",
                    lib_id,
                    self.name,
                    exc,
                )
                for flag in changed_flags:
                    results[f"{lib_id}:{flag}"] = f"error: {exc}"

        return results

    def parse_webhook(
        self,
        payload: dict[str, Any] | bytes,
        headers: dict[str, str],
    ) -> WebhookEvent | None:
        """Normalise an Emby Webhooks plugin payload to a :class:`WebhookEvent`.

        The plugin emits a Plex-format-compatible JSON envelope:
        ``{"Event": "library.new", "Item": {"Id": "..."}, "Server": {...}}``.
        We only act on ``library.new`` (or the equivalent ``ItemAdded``
        event some plugin builds emit); playback events are ignored.
        """
        if isinstance(payload, bytes | bytearray):
            try:
                data = json.loads(payload.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, ValueError):
                return None
        elif isinstance(payload, dict):
            data = payload
        else:
            return None

        if not isinstance(data, dict):
            return None

        event_type = str(data.get("Event") or data.get("event") or data.get("NotificationType") or "")
        if event_type.lower() not in {"library.new", "itemadded"}:
            return None

        item = data.get("Item") or data.get("Metadata") or {}
        item_id = str(item.get("Id") or item.get("guid") or "") if isinstance(item, dict) else ""

        return WebhookEvent(
            event_type=event_type,
            item_id=item_id or None,
            raw=data,
        )
