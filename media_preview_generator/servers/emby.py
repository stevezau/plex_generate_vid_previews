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

from ._embyish import EmbyApiClient, is_video_library_folder
from .base import FlagTarget, HealthCheckIssue, ServerType, WebhookEvent


class EmbyServer(EmbyApiClient):
    """Wrap a single Emby Server in the :class:`MediaServer` interface."""

    vendor_name = "Emby"

    def __init__(self, config) -> None:
        super().__init__(config, default_name="Emby")

    @property
    def type(self) -> ServerType:
        return ServerType.EMBY

    def _uncached_resolve_remote_path_to_item_id(self, remote_path: str) -> str | None:
        """Path → item id via Emby's exact-path filter on ``/Items``.

        Per the Emby team
        (https://emby.media/community/index.php?/topic/70680-search-item-by-file-path/),
        ``GET /Items?Path=<exact>&Recursive=true`` filters by the item's
        stored Path column — same indexed equality lookup the
        Jellyfin plugin uses internally, but exposed natively by Emby.
        Sub-millisecond on libraries of any size, immune to the
        searchTerm full-text index dropping tokens.

        This was confirmed working on Emby; it does NOT work on Jellyfin
        (the Jellyfin .NET-Core rewrite of ItemsController dropped the
        ``[FromQuery] string Path`` binding), which is why the Jellyfin
        client uses the Media Preview Bridge plugin instead.

        Falls back to the base class's library-scoped search when the
        exact-path query returns no hit.
        """
        if not remote_path:
            return None
        try:
            response = self._request(
                "GET",
                "/Items",
                params={
                    "Path": remote_path,
                    "Recursive": "true",
                    # Audit L2: restrict to Movie/Episode so a non-video
                    # item indexed at the same Path (e.g. an audiobook
                    # mistakenly classified as Audio with overlapping
                    # path layout) can't return a non-preview-worthy
                    # item id. The legacy ``_search_by_file_path`` in
                    # plex_client.py applies the same filter for the
                    # exact same reason.
                    "IncludeItemTypes": "Movie,Episode",
                    "Fields": "Path",
                    "Limit": 1,
                },
            )
            response.raise_for_status()
            items = response.json().get("Items") or []
            if items and isinstance(items[0], dict):
                item_id = str(items[0].get("Id") or "")
                if item_id:
                    return item_id
        except Exception as exc:
            logger.debug(
                "Emby exact-Path lookup failed for {!r}: {} — falling back to public API",
                remote_path,
                exc,
            )
        return super()._uncached_resolve_remote_path_to_item_id(remote_path)

    def _trigger_path_refresh(self, server_view_path: str) -> None:
        """Nudge Emby to scan a single server-view path.

        Calls ``POST /Library/Media/Updated`` which is Emby's
        path-based scan-nudge — same shape Sonarr/Radarr's own
        path-update notifier uses. Best-effort; failures are logged at
        debug level by the base wrapper.

        The base class (see
        :meth:`MediaServer.trigger_refresh`) calls this once per
        mapped candidate so multi-disk installs nudge every mount.
        """
        response = self._request(
            "POST",
            "/Library/Media/Updated",
            json_body={"Updates": [{"Path": server_view_path, "UpdateType": "Modified"}]},
        )
        response.raise_for_status()
        logger.info(
            "[{}] Triggered partial scan: {}",
            self.name,
            server_view_path,
        )

    def _trigger_path_deleted(self, server_view_path: str) -> None:
        """Tell Emby a previously-imported file is gone.

        Same ``/Library/Media/Updated`` endpoint as
        :meth:`_trigger_path_refresh`, but with ``UpdateType:"Deleted"``
        so Emby drops the stale library row instead of waiting for its
        filesystem monitor / scheduled scan to notice. Used after
        Radarr/Sonarr upgrade webhooks where the payload's
        ``deletedFiles[]`` lists the prior release that was replaced.

        Best-effort; failures are logged at debug level by the base
        wrapper.
        """
        response = self._request(
            "POST",
            "/Library/Media/Updated",
            json_body={"Updates": [{"Path": server_view_path, "UpdateType": "Deleted"}]},
        )
        response.raise_for_status()
        logger.info(
            "[{}] Notified deleted path: {}",
            self.name,
            server_view_path,
        )

    def _trigger_item_refresh(self, item_id: str) -> None:
        """Refresh metadata for a single Emby item id."""
        response = self._request("POST", f"/Items/{item_id}/Refresh")
        response.raise_for_status()
        logger.info(
            "[{}] Triggered item refresh: {}",
            self.name,
            item_id,
        )

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

    # The flag(s) that ``set_vendor_extraction`` flips for Emby. Both are
    # set to false by the apply path — chapter is the older Emby
    # mechanism, trickplay is Emby 4.8+. We treat both as
    # "vendor-stopped" when set to false; a library missing the
    # trickplay key (older Emby) is considered fine for that flag.
    _VENDOR_EXTRACTION_FLAGS: tuple[tuple[str, bool], ...] = (
        ("ExtractChapterImagesDuringLibraryScan", False),
        ("ExtractTrickplayImagesDuringLibraryScan", False),
    )

    def get_vendor_extraction_status(self) -> dict[str, int]:
        """Audit per-library vendor-extraction state without writing.

        Same shape as the Jellyfin variant but checks Emby's flag set.
        Older Emby installs that don't return ``ExtractTrickplayImagesDuringLibraryScan``
        in their LibraryOptions skip the audit for that flag (the field
        not being present means the library can't have it on, so it's
        already at the recommended state by absence).
        """
        try:
            response = self._request("GET", "/Library/VirtualFolders")
            response.raise_for_status()
            folders = response.json()
        except Exception as exc:
            logger.debug("Vendor-extraction status probe failed for {!r}: {}", self.name, exc)
            return {"extracting_count": 0, "stopped_count": 0, "skipped_count": 0, "total": 0}

        if not isinstance(folders, list):
            return {"extracting_count": 0, "stopped_count": 0, "skipped_count": 0, "total": 0}

        extracting = stopped = 0
        for raw in folders:
            if not isinstance(raw, dict):
                continue
            # Issue #237: skip music/photo/book libraries entirely —
            # they don't carry video preview options.
            if not is_video_library_folder(raw):
                continue
            options = raw.get("LibraryOptions") or {}
            all_recommended = True
            for flag, want in self._VENDOR_EXTRACTION_FLAGS:
                if flag not in options:
                    # Older Emby: field not present → can't be wrong.
                    continue
                if bool(options.get(flag, False)) != want:
                    all_recommended = False
                    break
            if all_recommended:
                stopped += 1
            else:
                extracting += 1

        return {
            "extracting_count": extracting,
            "stopped_count": stopped,
            "skipped_count": 0,
            "total": extracting + stopped,
        }

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
            # Issue #237: music/photo/book libraries don't expose the
            # preview flags — surfacing a HealthCheckIssue against them
            # tells users to disable a toggle that doesn't exist.
            if not is_video_library_folder(raw):
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

    # Per-flag UX metadata. Every Emby flag flip is reversible (sidecars
    # aren't deleted by any change), so confirm.kind is always "button"
    # and the body explains what the action does + what it costs.
    _FLAG_METADATA: dict[str, dict[str, Any]] = {
        "ExtractTrickplayImagesDuringLibraryScan": {
            "check_id": "scan_extraction",
            "docs_anchor": "scan-extraction",
            "tooltip": "Emby's own scan-time trickplay generation",
            "explanation": (
                "<p><strong>What it does:</strong> on Emby 4.8+, controls whether Emby generates "
                "its own trickplay tiles during library scans.</p>"
                "<p><strong>Why we recommend off:</strong> this app owns preview generation "
                "(GPU-accelerated, HDR-aware, with frame-reuse caching). Letting Emby also "
                "extract tiles during scans is pure duplicate CPU — both sets land in the same "
                "place and whichever registers first wins, but you've burned twice the work.</p>"
                "<p><strong>What happens if you enable it:</strong> Emby starts generating its own "
                "trickplay tiles on every scan in parallel to this app. Nothing breaks, but scans "
                "get longer and CPU usage during scans roughly doubles.</p>"
            ),
            "enable_body": (
                "Re-enables Emby's scan-time trickplay generation. Emby will generate its OWN "
                "preview tiles in parallel to this app on every library scan — duplicate work, "
                "but nothing is deleted. Useful only if you plan to stop using this app for "
                "Emby previews."
            ),
            "disable_body": (
                "Stops Emby from generating its own trickplay tiles during library scans. "
                "This app keeps publishing previews the same way — Emby just stops doing "
                "duplicate work. Non-destructive and reversible."
            ),
        },
        "ExtractChapterImagesDuringLibraryScan": {
            "check_id": "chapter_extraction",
            "docs_anchor": "chapter-extraction",
            "tooltip": "Emby's older chapter-image preview pipeline",
            "explanation": (
                "<p><strong>What it does:</strong> generates chapter-image thumbnails during "
                "library scans — the preview mechanism Emby used before 4.8 introduced "
                "trickplay tiles.</p>"
                "<p><strong>Why we recommend off:</strong> chapter images aren't what this app "
                "publishes (we write trickplay tiles). Leaving chapter-image extraction on means "
                "Emby burns CPU during every scan generating thumbnails nothing in this pipeline "
                "reads or displays.</p>"
                "<p><strong>What happens if you enable it:</strong> Emby generates chapter images "
                "during scans. They don't replace or conflict with this app's trickplay tiles — "
                "just wasted CPU. Reversible any time.</p>"
            ),
            "enable_body": (
                "Re-enables Emby's chapter-image extraction during library scans. Legacy Emby "
                "preview mechanism; doesn't affect trickplay tiles this app publishes. Just "
                "costs CPU during scans."
            ),
            "disable_body": (
                "Stops Emby generating chapter-image thumbnails during scans. This app's "
                "trickplay tiles aren't affected — they're a separate mechanism. Non-destructive "
                "and reversible."
            ),
        },
        "EnableRealtimeMonitor": {
            "check_id": "realtime_monitor",
            "docs_anchor": "realtime-monitor",
            "tooltip": "Auto-detect new files (real-time monitoring)",
            "explanation": (
                "<p><strong>What it does:</strong> tells Emby to watch the library's folder tree "
                "for filesystem changes (new files, moves, renames) and pick them up "
                "immediately instead of waiting for the next scheduled scan.</p>"
                "<p><strong>Why we recommend on:</strong> Sonarr/Radarr imports a file → Emby "
                "notices within seconds → this app's webhook fires → preview gets generated and "
                "published, total latency in seconds. Off means everything stalls until the next "
                "scheduled scan or a webhook nudge from this app.</p>"
                "<p><strong>What happens if you disable it:</strong> new files won't show up in "
                "Emby — or trigger preview generation — until a manual scan runs. Non-destructive; "
                "re-enabling any time restores the instant-notification flow.</p>"
            ),
            "enable_body": (
                "Emby will watch the library folders and auto-detect new files instantly. "
                "Recommended for fast preview generation after Sonarr/Radarr imports."
            ),
            "disable_body": (
                "Emby will stop watching the filesystem for new files. New episodes/movies "
                "imported by Sonarr/Radarr won't show up in Emby — or trigger preview "
                "generation — until a manual scan runs. Reversible any time."
            ),
        },
    }

    def _flag_actions(self, flag: str, current: bool) -> dict[str, Any]:
        """Build ``actions`` blob — expose the toggle that changes state,
        and attach a button-confirm blob with the enable/disable body so
        users see what they're about to do before clicking.
        """
        meta = self._FLAG_METADATA.get(flag, {})
        actions: dict[str, Any] = {}
        if not current:
            actions["enable"] = {
                "action": "apply_flag",
                "args": {"flag": flag, "value": True},
                "confirm": {
                    "kind": "button",
                    "phrase": "",
                    "body": meta.get("enable_body") or "",
                },
            }
        if current:
            actions["disable"] = {
                "action": "apply_flag",
                "args": {"flag": flag, "value": False},
                "confirm": {
                    "kind": "button",
                    "phrase": "",
                    "body": meta.get("disable_body") or "",
                },
            }
        return actions

    def apply_flag_values(self, targets: list[FlagTarget]) -> dict[str, str]:
        """Set each ``(flag, value)`` pair explicitly across libraries.

        Same wholesale-replace pattern as
        :meth:`apply_recommended_settings` but accepts explicit values
        so users can flip flags AWAY from the recommended state.
        Older-Emby libraries that don't expose ``ExtractTrickplayImagesDuringLibraryScan``
        skip that flag silently (it can't be mis-set if it's not present).
        """
        if not targets:
            return {}

        try:
            response = self._request("GET", "/Library/VirtualFolders")
            response.raise_for_status()
            folders = response.json()
        except Exception as exc:
            return {"_global": f"failed to fetch libraries: {exc}"}

        if not isinstance(folders, list):
            return {"_global": "unexpected VirtualFolders response shape"}

        per_flag: dict[str, list[FlagTarget]] = {}
        for target in targets:
            flag = str(target.get("flag") or "")
            if not flag:
                continue
            per_flag.setdefault(flag, []).append(target)

        results: dict[str, str] = {}
        for raw in folders:
            if not isinstance(raw, dict):
                continue
            # Issue #237: music/photo libraries have no video preview
            # flags to flip. Even if the UI never sends a target for
            # them, defend at the apply-site too.
            if not is_video_library_folder(raw):
                continue
            lib_id = str(raw.get("ItemId") or raw.get("Id") or raw.get("Name") or "")
            options = dict(raw.get("LibraryOptions") or {})

            changed: list[str] = []
            for flag, target_rows in per_flag.items():
                # Older Emby: skip 4.8+ trickplay flag when the library
                # can't hold it.
                if flag == "ExtractTrickplayImagesDuringLibraryScan" and flag not in options:
                    continue
                chosen: FlagTarget | None = None
                for row in target_rows:
                    lib_ids = row.get("library_ids")
                    if lib_ids is None:
                        if chosen is None:
                            chosen = row
                    elif lib_id in lib_ids:
                        chosen = row
                        break
                if chosen is None:
                    continue
                want = chosen.get("value")
                if isinstance(want, str):
                    want = want.lower() in ("true", "1", "yes", "on")
                if bool(options.get(flag, False)) == bool(want):
                    continue
                options[flag] = bool(want)
                changed.append(flag)

            if not changed:
                continue

            try:
                update = self._request(
                    "POST",
                    "/Library/VirtualFolders/LibraryOptions",
                    json_body={"Id": lib_id, "LibraryOptions": options},
                )
                update.raise_for_status()
                for flag in changed:
                    results[f"{lib_id}:{flag}"] = "ok"
            except Exception as exc:
                logger.warning(
                    "Could not update Emby library {} settings on server {!r}: {}",
                    lib_id,
                    self.name,
                    exc,
                )
                for flag in changed:
                    results[f"{lib_id}:{flag}"] = f"error: {exc}"

        return results

    def previews_readiness(self) -> dict[str, Any]:
        """Unified readiness payload for the Previews readiness card.

        Returns the envelope documented on
        :meth:`MediaServer.previews_readiness`. Emby sections:
        ``connection``, ``version``, ``library_settings``,
        ``vendor_extraction``. Emby has no plugin architecture and no
        server-wide trickplay geometry knob, so those sections are
        absent.

        Emby's sidecar auto-discovery works purely by filename
        convention — every library-flag issue is advisory (wasted-CPU
        warning) and nothing blocks preview playback, so
        ``overall_ok`` is always True.
        """
        sections: list[dict[str, Any]] = []

        version_value = ""
        connection_ok = True
        connection_reason = ""
        try:
            response = self._request("GET", "/System/Info")
            response.raise_for_status()
            data = response.json() or {}
            version_value = str(data.get("Version") or "")
        except Exception as exc:
            logger.debug("Version probe failed for {!r}: {}", self.name, exc)
            connection_ok = False
            connection_reason = f"Could not read /System/Info: {exc}"

        sections.append(
            {
                "id": "connection",
                "title": "Connection",
                "docs_anchor": "connection",
                "ok": connection_ok,
                "severity": "critical",
                "checks": [
                    {
                        "id": "reachable",
                        "label": "Emby reachable",
                        "docs_anchor": "connection",
                        "tooltip": "Server is reachable and responds to API calls",
                        "explanation": (
                            "<p><strong>What it checks:</strong> this app sent a GET to "
                            "<code>/System/Info</code> on the configured Emby URL and got back a "
                            "successful JSON response.</p>"
                            "<p><strong>Why it matters:</strong> every downstream check depends "
                            "on talking to Emby. If this fails, the rest of the card is "
                            "meaningless.</p>"
                            "<p><strong>Common causes when it fails:</strong> wrong URL (e.g. "
                            "<code>localhost</code> from inside this container), expired API "
                            "key, Emby restarting, or a network issue. Read-only check — fix "
                            "the URL/credentials in the General tab.</p>"
                        ),
                        "ok": connection_ok,
                        "severity": "critical",
                        "current": "reachable" if connection_ok else "unreachable",
                        "recommended": "reachable",
                        "actions": {},
                        "reason": connection_reason,
                        "meta": {},
                    }
                ],
            }
        )

        sections.append(
            {
                "id": "version",
                "title": "Server version",
                "docs_anchor": "version",
                "ok": True,
                "severity": "info",
                "checks": [
                    {
                        "id": "server_version",
                        "label": f"Emby {version_value}" if version_value else "Emby version",
                        "docs_anchor": "version",
                        "tooltip": "Informational — any recent Emby release works",
                        "explanation": (
                            "<p><strong>What it reports:</strong> Emby's self-reported version "
                            "from <code>/System/Info</code>.</p>"
                            "<p><strong>Why it's informational:</strong> Emby's sidecar BIF "
                            "auto-discovery (how this app's previews become visible) works by "
                            "filename convention — no minimum-version gate. Any recent Emby "
                            "release supports it out of the box.</p>"
                            "<p><strong>When it would matter:</strong> if Emby ever ships a "
                            "major version that breaks sidecar discovery, this row will surface "
                            "the version and recommend an action. Read-only check.</p>"
                        ),
                        "ok": True,
                        "severity": "info",
                        "current": version_value or "unknown",
                        "recommended": None,
                        "actions": {},
                        "reason": None,
                        "meta": {},
                    }
                ],
            }
        )

        # --- Library settings — per-library per-flag rows ------------
        library_checks: list[dict[str, Any]] = []
        library_section_ok = True
        try:
            response = self._request("GET", "/Library/VirtualFolders")
            response.raise_for_status()
            folders = response.json()
        except Exception as exc:
            logger.debug("Library flags probe failed for {!r}: {}", self.name, exc)
            folders = []

        any_modern_trickplay_flag = False
        any_libraries_seen = False
        if isinstance(folders, list):
            for raw in folders:
                if not isinstance(raw, dict):
                    continue
                # Issue #237: skip music/photo/book libraries — they don't
                # have video preview flags, so emitting per-flag rows for
                # them tells users to disable settings that don't exist.
                if not is_video_library_folder(raw):
                    continue
                any_libraries_seen = True
                lib_id = str(raw.get("ItemId") or raw.get("Id") or raw.get("Name") or "")
                lib_name = str(raw.get("Name") or "")
                options = raw.get("LibraryOptions") or {}
                if "ExtractTrickplayImagesDuringLibraryScan" in options:
                    any_modern_trickplay_flag = True
                for flag, label, recommended, severity, rationale in self._RECOMMENDED_SETTINGS:
                    if flag == "ExtractTrickplayImagesDuringLibraryScan" and flag not in options:
                        continue
                    current = bool(options.get(flag, False))
                    row_ok = current == recommended
                    if not row_ok:
                        library_section_ok = False
                    meta = self._FLAG_METADATA.get(flag, {})
                    actions = self._flag_actions(flag, current)
                    for key in ("enable", "disable"):
                        if key in actions:
                            actions[key]["args"] = {
                                **actions[key]["args"],
                                "library_ids": [lib_id] if lib_id else None,
                            }
                    library_checks.append(
                        {
                            "id": f"{meta.get('check_id', flag)}:{lib_id}",
                            "label": f"{lib_name or 'library'} — {label}",
                            "docs_anchor": meta.get("docs_anchor", "library-settings"),
                            "tooltip": meta.get("tooltip", rationale),
                            "explanation": meta.get("explanation")
                            or f"<p>{meta.get('tooltip', '') or ''}</p><p>{rationale}</p>",
                            "ok": row_ok,
                            "severity": severity,
                            "current": current,
                            "recommended": recommended,
                            "actions": actions,
                            "reason": None if row_ok else rationale,
                            "meta": {"flag": flag, "library_id": lib_id, "library_name": lib_name},
                        }
                    )

        # Emby <4.8 doesn't expose ExtractTrickplayImagesDuringLibraryScan
        # at all. Silently skipping the check (line above) made libraries
        # show fewer rows on older Embys with no explanation — users
        # wondered if a config change had eaten the row. Surface a single
        # info note so the gap is intentional and visible.
        if any_libraries_seen and not any_modern_trickplay_flag:
            library_checks.append(
                {
                    "id": "trickplay_flag_unavailable",
                    "label": "Trickplay extraction toggle not available",
                    "docs_anchor": "library-settings",
                    "tooltip": "This Emby version is older than 4.8",
                    "explanation": (
                        "<p><strong>What this means:</strong> the per-library "
                        "<code>ExtractTrickplayImagesDuringLibraryScan</code> flag "
                        "was added in Emby 4.8. Older versions don't expose it, so "
                        "this app can't toggle Emby's scan-time trickplay extraction "
                        "for you.</p>"
                        "<p><strong>Impact:</strong> sidecar BIF previews this app "
                        "publishes still work — Emby auto-discovers them by filename "
                        "convention regardless of version. Upgrade to Emby 4.8+ if "
                        "you want the toggle exposed here.</p>"
                    ),
                    "ok": True,
                    "severity": "info",
                    "current": "unavailable on this Emby version",
                    "recommended": None,
                    "actions": {},
                    "reason": None,
                    "meta": {"flag": "ExtractTrickplayImagesDuringLibraryScan"},
                }
            )

        sections.append(
            {
                "id": "library_settings",
                "title": "Library settings",
                "docs_anchor": "library-settings",
                "ok": library_section_ok,
                "severity": "recommended" if not library_section_ok else "info",
                "checks": library_checks,
            }
        )

        # --- Vendor-side extraction ---------------------------------
        try:
            extraction_status = self.get_vendor_extraction_status()
        except Exception as exc:
            logger.debug("Vendor-extraction status probe failed for {!r}: {}", self.name, exc)
            extraction_status = {"extracting_count": 0, "stopped_count": 0, "skipped_count": 0, "total": 0}
        stopped = extraction_status.get("stopped_count", 0)
        extracting = extraction_status.get("extracting_count", 0)
        vendor_current = f"stopped on {stopped}/{stopped + extracting}" if (stopped + extracting) else "unknown"
        sections.append(
            {
                "id": "vendor_extraction",
                "title": "Vendor-side preview generation",
                "docs_anchor": "vendor-extraction",
                "ok": True,
                "severity": "info",
                "checks": [
                    {
                        "id": "vendor_extraction_state",
                        "label": "Emby scan-time extraction",
                        "docs_anchor": "vendor-extraction",
                        "tooltip": "Stop Emby running its own preview extraction",
                        "explanation": (
                            "<p><strong>What this controls:</strong> a server-wide shortcut for "
                            "disabling Emby's own preview extraction (trickplay + chapter images) "
                            "across every configured library in one batch.</p>"
                            "<p><strong>Why we recommend stopping it:</strong> this app handles "
                            "preview generation end-to-end (GPU-accelerated, HDR-aware, frame-"
                            "reuse caching). Letting Emby ALSO extract during scans is "
                            "duplicate CPU — both sets of images land in the same place, but "
                            "you've burned twice the work.</p>"
                            "<p><strong>What happens if you re-enable:</strong> Emby starts "
                            "extracting its own preview images during library scans in parallel "
                            "to this app. Wasteful but non-destructive.</p>"
                        ),
                        "ok": True,
                        "severity": "info",
                        "current": vendor_current,
                        "recommended": "stopped",
                        "actions": {
                            "disable": {
                                "action": "set_vendor_extraction",
                                "args": {"scan_extraction": False},
                                "confirm": {
                                    "kind": "button",
                                    "phrase": "",
                                    "body": (
                                        "Stops Emby running its own trickplay + chapter-image "
                                        "extraction during library scans across all libraries. "
                                        "Recommended when this app owns preview generation. "
                                        "Non-destructive — existing previews stay on disk and "
                                        "continue to work."
                                    ),
                                },
                            },
                            "enable": {
                                "action": "set_vendor_extraction",
                                "args": {"scan_extraction": True},
                                "confirm": {
                                    "kind": "button",
                                    "phrase": "",
                                    "body": (
                                        "Re-enables Emby's scan-time preview extraction across "
                                        "all libraries. Emby will generate its OWN preview "
                                        "images in parallel to this app — duplicate CPU, no "
                                        "data loss. Useful only if you plan to stop using this "
                                        "app for Emby previews."
                                    ),
                                },
                            },
                        },
                        "reason": None,
                        "meta": extraction_status,
                    }
                ],
            }
        )

        # --- Scheduled "Generate Trickplay Images" task --------------
        # Emby has no Bridge-plugin equivalent — this app publishes
        # sidecar tile files and relies on Emby's filename-based
        # auto-discovery (the daily scheduled task is what wires that
        # discovery up). Disabling the task breaks the registration
        # path entirely, so the recommendation here is always "keep
        # enabled" — purely informational on Emby.
        sched_state = self.get_scheduled_trickplay_state()
        if sched_state.get("found"):
            triggers_count = int(sched_state.get("triggers_count") or 0)
            task_running = (sched_state.get("state") or "").lower() == "running"
            sched_explanation = (
                "<p><strong>What this task does:</strong> Emby's built-in "
                "<code>Generate Trickplay Images</code> scheduled task scans every video "
                "in your libraries and ingests sidecar trickplay tiles (the ones this app "
                "publishes) into Emby's database so the player can serve them.</p>"
                "<p><strong>Why it matters on Emby:</strong> unlike Jellyfin (which has a "
                "Bridge-plugin path for instant registration), Emby's only registration "
                "path is this scheduled task. This app writes the tile files next to your "
                "media; Emby's daily task discovers them and wires them up. Disable the "
                "task and tiles sit on disk indefinitely — trickplay never appears in the "
                "player.</p>"
                "<p><strong>Recommendation:</strong> keep this enabled on Emby.</p>"
            )
            if triggers_count > 0:
                running_note = " (currently running)" if task_running else ""
                sched_check = {
                    "id": "scheduled_trickplay_task",
                    "label": "Emby's daily 'Generate Trickplay Images' task",
                    "docs_anchor": "scheduled-trickplay",
                    "tooltip": "Keep enabled — Emby's only path to register the tiles this app publishes.",
                    "explanation": sched_explanation,
                    "ok": True,
                    "severity": "info",
                    "current": f"enabled ({triggers_count} trigger{'s' if triggers_count != 1 else ''}){running_note}",
                    "recommended": "keep enabled",
                    "actions": {},
                    "reason": None,
                    "meta": sched_state,
                }
                sched_section_ok = True
                sched_section_severity = "info"
            else:
                sched_check = {
                    "id": "scheduled_trickplay_task",
                    "label": "Emby's daily 'Generate Trickplay Images' task",
                    "docs_anchor": "scheduled-trickplay",
                    "tooltip": (
                        "Critical: Emby has no other way to discover the tiles this app "
                        "publishes — trickplay will never appear in the player."
                    ),
                    "explanation": (
                        sched_explanation + "<p><strong>Your setup:</strong> the task has no triggers. Tiles "
                        "this app publishes will never be registered. Re-enable the task in "
                        "Emby → Dashboard → Scheduled Tasks.</p>"
                    ),
                    "ok": False,
                    "severity": "critical",
                    "current": "disabled (no triggers)",
                    "recommended": "enabled (Emby has no other registration path)",
                    # Recommended fix on Emby = re-enable. Without the
                    # explicit hint, the JS direction-picker would treat
                    # the string ``recommended`` as truthy and pick the
                    # ``enable`` action by accident — same outcome in
                    # this case but lucky, not principled. Pin it.
                    "fix_action": "enable",
                    "actions": {
                        "enable": {
                            "action": "set_scheduled_trickplay",
                            "args": {"enabled": True},
                            "confirm": {
                                "kind": "button",
                                "phrase": "",
                                "body": (
                                    "Restores the default daily 3 AM trigger. Without this "
                                    "task running, Emby will never discover the tiles this "
                                    "app publishes — trickplay will not appear in the player."
                                ),
                            },
                        },
                    },
                    "reason": None,
                    "meta": sched_state,
                }
                sched_section_ok = False
                sched_section_severity = "critical"

            sections.append(
                {
                    "id": "scheduled_trickplay",
                    "title": "Scheduled trickplay task",
                    "docs_anchor": "scheduled-trickplay",
                    "ok": sched_section_ok,
                    "severity": sched_section_severity,
                    "checks": [sched_check],
                }
            )
        else:
            sched_section_ok = True

        return {
            "vendor": "emby",
            # Emby library-flag issues are advisory; scheduled-task absence
            # breaks registration entirely, so it joins overall_ok.
            "overall_ok": connection_ok and sched_section_ok,
            "sections": sections,
        }

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
            # Issue #237: don't flip flags on music/photo/book libraries
            # — they shouldn't have appeared in the UI list in the first
            # place, but the apply-site guards independently.
            if not is_video_library_folder(raw):
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
        # Emby's library.new payload includes the local file path in
        # ``Item.Path``. Capturing it lets the dispatcher skip an extra
        # reverse-lookup roundtrip per webhook (audit fix — was being
        # silently dropped).
        item_path = str(item.get("Path") or item.get("path") or "").strip() or None if isinstance(item, dict) else None

        return WebhookEvent(
            event_type=event_type,
            item_id=item_id or None,
            remote_path=item_path,
            raw=data,
        )
