"""Jellyfin implementation of the :class:`MediaServer` interface.

Most of the REST surface is shared with Emby (Jellyfin forked from
Emby) and lives in :mod:`._embyish`. This module specialises that base
with the Jellyfin-only bits:

* :class:`ServerType` enum value.
* :meth:`trigger_refresh` — prefers Jellyfin's path-based
  ``/Library/Media/Updated`` (same shape as Emby's; the inherited
  Emby docstring used to claim Jellyfin lacked this — it doesn't),
  falls back to ``/Items/{id}/Refresh`` and finally the rate-limited
  full ``/Library/Refresh``.
* :meth:`parse_webhook` — jellyfin-plugin-webhook payload shape
  (``NotificationType`` / ``ItemId`` / ``ServerId``).

The Quick Connect auth flow lives separately in
:mod:`.jellyfin_auth` because it's only used during setup, not
during normal operation.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from loguru import logger

from ._embyish import EmbyApiClient
from .base import HealthCheckIssue, ServerType, WebhookEvent

# Floor on how often a single Jellyfin server may receive a full
# /Library/Refresh nudge. Without it, a webhook burst (e.g. Sonarr
# importing a season pack) would trigger one full library scan per
# file — pinning the Jellyfin process for minutes. 60s comfortably
# covers the typical Jellyfin scan cadence and keeps the publisher
# retry-loop responsive.
_JELLYFIN_FULL_REFRESH_COOLDOWN_S = 60.0


class JellyfinServer(EmbyApiClient):
    """Wrap a single Jellyfin server in the :class:`MediaServer` interface.

    Args:
        config: Persisted configuration. ``config.auth`` accepts any of:

            - ``{"method": "quick_connect", "access_token": "...", "user_id": "..."}``
            - ``{"method": "password", "access_token": "...", "user_id": "..."}``
            - ``{"method": "api_key", "api_key": "..."}``

            The token (whichever flow produced it) goes out on the
            ``X-Emby-Token`` header — Jellyfin honours the legacy Emby
            header name alongside the modern ``Authorization`` form.
    """

    vendor_name = "Jellyfin"

    def __init__(self, config) -> None:
        super().__init__(config, default_name="Jellyfin")
        self._last_full_refresh_at = 0.0
        self._full_refresh_lock = threading.Lock()

    @property
    def type(self) -> ServerType:
        return ServerType.JELLYFIN

    def _trigger_path_refresh(self, server_view_path: str) -> None:
        """Nudge Jellyfin to scan a single server-view path.

        Calls ``POST /Library/Media/Updated`` (same shape as Emby's,
        despite Jellyfin's docs claiming otherwise) — feeds the path
        into Jellyfin's library monitor as if real-time-monitor's
        inotify watcher had posted it. Per-file, no global scan, no
        cooldown needed.

        On failure (4xx/5xx), falls back to a rate-limited full
        ``/Library/Refresh`` so we still nudge the server even when
        the path-based endpoint is unavailable. The base wrapper logs
        any exception this method raises at debug level.

        The base class (see :meth:`MediaServer.trigger_refresh`) calls
        this once per mapped candidate so multi-disk installs nudge
        every mount.
        """
        try:
            response = self._request(
                "POST",
                "/Library/Media/Updated",
                json_body={"Updates": [{"Path": server_view_path, "UpdateType": "Created"}]},
            )
            response.raise_for_status()
            logger.debug(
                "Jellyfin /Library/Media/Updated nudged scan for {}",
                server_view_path,
            )
            return
        except Exception as exc:
            logger.debug(
                "Jellyfin /Library/Media/Updated failed for {}: {} — falling back to /Library/Refresh",
                server_view_path,
                exc,
            )

        self._maybe_trigger_full_refresh()

    def _trigger_item_refresh(self, item_id: str) -> None:
        """Refresh metadata + register published trickplay for one item.

        Two best-effort steps:

        1. ``POST /MediaPreviewBridge/Trickplay/{itemId}`` — the
           Jellyfin Plugin Bridge installed alongside this tool.
           Registers the trickplay row directly via Jellyfin's
           ``ITrickplayManager.SaveTrickplayInfo`` so the player can
           serve scrubbing previews immediately, no flag flips and no
           ffmpeg. Returns 404 if the plugin isn't installed —
           silently swallowed so single-step setups still work.

        2. ``POST /Items/{id}/Refresh`` — standard metadata refresh.
           On failure, falls back to a rate-limited full
           ``/Library/Refresh``.

        The base wrapper logs any exception this method raises.
        """
        # 1. Plugin bridge — instant trickplay registration.
        # Audit L1: width / intervalMs MUST match the
        # JellyfinTrickplayAdapter's configured values, NOT hardcoded
        # defaults. Without this, a user with a non-default
        # ``output.width`` (e.g. 480) writes tiles into
        # ``<basename>.trickplay/480 - 10x10/`` but the plugin gets
        # asked to register ``<basename>.trickplay/320 - 10x10/`` —
        # which doesn't exist → 404 → silent registration miss → the
        # user-visible delay drops from "instant" to "next 3 AM scan".
        # Plugin's controller verifies the width-specific tile dir
        # exists before committing the trickplay row.
        output = (self._config.output or {}) if getattr(self, "_config", None) else {}
        adapter_width = int(output.get("width") or 320)
        # Plugin expects intervalMs (milliseconds); the adapter's
        # ``frame_interval`` is in seconds. Convert here so the two
        # views agree: tiles named "<width> - 10x10/<index>.jpg"
        # match the row registered with the matching intervalMs.
        adapter_interval_ms = int(output.get("frame_interval") or 10) * 1000
        try:
            resp = self._request(
                "POST",
                f"/MediaPreviewBridge/Trickplay/{item_id}",
                params={"width": adapter_width, "intervalMs": adapter_interval_ms},
            )
            if resp.status_code == 204:
                logger.debug(
                    "Jellyfin trickplay registered via Media Preview Bridge plugin for {}",
                    item_id,
                )
            elif resp.status_code == 404:
                logger.debug(
                    "Media Preview Bridge plugin not installed on Jellyfin {!r} — "
                    "trickplay will be picked up by the next library scan instead.",
                    self.name,
                )
            else:
                logger.debug(
                    "Media Preview Bridge plugin returned HTTP {} for {}: {}",
                    resp.status_code,
                    item_id,
                    resp.text[:200] if resp.text else "",
                )
        except Exception as exc:
            logger.debug(
                "Media Preview Bridge plugin call failed for {}: {}",
                item_id,
                exc,
            )

        # 2. Standard metadata refresh (separate concern from trickplay).
        try:
            response = self._request("POST", f"/Items/{item_id}/Refresh")
            response.raise_for_status()
            return
        except Exception as exc:
            logger.debug("Jellyfin per-item refresh failed for {}: {}", item_id, exc)

        self._maybe_trigger_full_refresh()

    def _maybe_trigger_full_refresh(self) -> None:
        """Last-resort rate-limited full ``/Library/Refresh``.

        Without rate-limiting, a webhook burst (Sonarr season-pack
        import) would trigger one full library scan per file — pins
        Jellyfin for minutes and outpaces what a real scan can cover.
        """
        with self._full_refresh_lock:
            now = time.monotonic()
            elapsed = now - self._last_full_refresh_at
            if elapsed < _JELLYFIN_FULL_REFRESH_COOLDOWN_S:
                logger.debug(
                    "Jellyfin /Library/Refresh suppressed for {!r} — last scan {:.0f}s ago, cooldown {:.0f}s",
                    self.name,
                    elapsed,
                    _JELLYFIN_FULL_REFRESH_COOLDOWN_S,
                )
                return
            self._last_full_refresh_at = now
        try:
            response = self._request("POST", "/Library/Refresh")
            response.raise_for_status()
        except Exception as exc:
            logger.debug("Jellyfin /Library/Refresh failed: {}", exc)

    def _uncached_resolve_remote_path_to_item_id(self, remote_path: str) -> str | None:
        """Path → item id with the Media Preview Bridge plugin shortcut.

        Tries the plugin's ``GET /MediaPreviewBridge/ResolvePath`` first.
        That endpoint calls ``ILibraryManager.FindByPath`` internally —
        a single equality lookup against the indexed Path column on
        Jellyfin's BaseItems table (sub-millisecond on libraries of any
        size, and immune to the searchTerm full-text index dropping
        tokens like 4K / HDR / DV / release-group brackets).

        Falls back to the library-scoped ``searchTerm`` + enumeration
        path on the base class when the plugin isn't installed (404)
        or any other transport error. Caller-side cache wraps both
        paths so repeat queries within the TTL are free.
        """
        if not remote_path:
            return None
        try:
            response = self._request(
                "GET",
                "/MediaPreviewBridge/ResolvePath",
                params={"path": remote_path},
            )
        except Exception as exc:
            logger.debug(
                "Media Preview Bridge ResolvePath failed for {!r}: {} — falling back to public API",
                remote_path,
                exc,
            )
            return super()._uncached_resolve_remote_path_to_item_id(remote_path)
        if response.status_code == 200:
            try:
                payload = response.json()
                item_id = str(payload.get("itemId") or "")
                if item_id:
                    return item_id
                # Plugin returned 200 but with an empty / missing
                # itemId. Two known shapes: (a) a malformed plugin
                # response, (b) plugin installed but unable to resolve.
                # Either way the caller falls through to the base
                # class (Pass 0/1/2) — log it so a buggy plugin
                # silently re-paying the Pass-0 cost on every webhook
                # is visible (final-audit LOW finding).
                logger.debug(
                    "Media Preview Bridge ResolvePath returned 200 with empty itemId for {!r} "
                    "— falling back to base resolver. Plugin may be misconfigured.",
                    remote_path,
                )
            except (ValueError, AttributeError) as exc:
                logger.debug(
                    "Media Preview Bridge ResolvePath returned bad JSON for {!r}: {}",
                    remote_path,
                    exc,
                )
        elif response.status_code == 404:
            # Two possible meanings: (a) plugin not installed (no route
            # registered), or (b) plugin installed and definitively no
            # item at this path. Either way, fall through to the base
            # class — its library-prefix short-circuit handles (b)
            # correctly in microseconds, and its scoped-search handles
            # (a) without the user seeing a delay.
            return super()._uncached_resolve_remote_path_to_item_id(remote_path)
        else:
            logger.debug(
                "Media Preview Bridge ResolvePath returned HTTP {} for {!r} — falling back",
                response.status_code,
                remote_path,
            )
        return super()._uncached_resolve_remote_path_to_item_id(remote_path)

    # ------------------------------------------------------------------
    # Media Preview Bridge plugin — install + status helpers
    # ------------------------------------------------------------------
    #
    # The plugin (jellyfin-plugin/) lets us register externally-published
    # trickplay with Jellyfin's TrickplayInfos store via a single internal
    # API call. With it installed we get instant scrubbing previews + zero
    # ffmpeg burn for items we cover. Without it, our trickplay still
    # works eventually via Jellyfin's daily 3 AM scheduled task — but
    # with the plugin the user-visible delay drops from 24h to 0s.

    PLUGIN_NAME = "Media Preview Bridge"
    PLUGIN_GUID = "c2cb9bf9-7c5d-4f1a-9a07-2d6f5e5b0001"
    PLUGIN_REPO_URL = "https://stevezau.github.io/media_preview_generator/jellyfin-plugin/manifest.json"

    def check_plugin_installed(self) -> dict[str, Any]:
        """Probe the plugin's anonymous Ping endpoint.

        Returns a dict with:

          * ``installed`` — bool, True when the plugin returns 200 OK.
          * ``version`` — plugin version string when installed, else empty.
          * ``error`` — short description of the failure mode for the UI
            (e.g. ``"timeout"``, ``"404"``, ``"connection refused"``).

        Tolerant to all transport failures — connection-test code paths
        call this and the user shouldn't see a stack trace just because
        the plugin isn't installed yet.
        """
        try:
            response = self._request("GET", "/MediaPreviewBridge/Ping")
        except Exception as exc:
            return {"installed": False, "version": "", "error": f"{type(exc).__name__}: {exc}"[:200]}
        if response.status_code != 200:
            return {"installed": False, "version": "", "error": f"HTTP {response.status_code}"}
        try:
            payload = response.json()
            return {
                "installed": bool(payload.get("ok")),
                "version": str(payload.get("version") or ""),
                "error": "",
            }
        except (ValueError, AttributeError) as exc:
            return {"installed": False, "version": "", "error": f"bad JSON: {exc}"[:200]}

    def install_plugin(self) -> dict[str, Any]:
        """One-click install: register repo, install package, restart Jellyfin.

        Steps, all best-effort with structured errors so the UI can
        surface progress:

        1. ``GET /Repositories`` — read existing repo list.
        2. ``POST /Repositories`` — append our plugin's manifest URL if
           it's not already there.
        3. ``POST /Packages/Installed/{name}?assemblyGuid=…&repositoryUrl=…``
           — queue the install (Jellyfin downloads the DLL on the next
           sweep, typically <30s).
        4. ``POST /System/Restart`` — Jellyfin loads new plugins on
           startup, so a restart is required for the install to take
           effect.

        Caller is responsible for polling :meth:`check_plugin_installed`
        after this returns to know when the restart finished + the
        plugin is live (Jellyfin restarts asynchronously; takes ~10–30s
        on a typical install).
        """
        result: dict[str, Any] = {"steps": [], "ok": False, "error": ""}

        def _record(step: str, ok: bool, detail: str = "") -> None:
            result["steps"].append({"step": step, "ok": ok, "detail": detail})

        # 1. Read existing repos.
        try:
            response = self._request("GET", "/Repositories")
            response.raise_for_status()
            repos = response.json() or []
        except Exception as exc:
            result["error"] = f"could not read repositories: {exc}"
            _record("read_repositories", False, str(exc))
            return result
        if not isinstance(repos, list):
            result["error"] = f"unexpected /Repositories shape: {type(repos).__name__}"
            _record("read_repositories", False, result["error"])
            return result
        _record("read_repositories", True, f"{len(repos)} existing")

        # 2. Append our repo if missing.
        if not any(isinstance(r, dict) and r.get("Url") == self.PLUGIN_REPO_URL for r in repos):
            new_repos = list(repos)
            new_repos.append(
                {
                    "Name": "Media Preview Bridge",
                    "Url": self.PLUGIN_REPO_URL,
                    "Enabled": True,
                }
            )
            try:
                self._request("POST", "/Repositories", json_body=new_repos).raise_for_status()
                _record("add_repository", True, "appended")
            except Exception as exc:
                result["error"] = f"could not add repository: {exc}"
                _record("add_repository", False, str(exc))
                return result
        else:
            _record("add_repository", True, "already present")

        # 3. Trigger install. Jellyfin downloads asynchronously — POST
        # only queues the job. The package name in the URL must match
        # the manifest's ``name`` field exactly.
        from urllib.parse import quote

        try:
            self._request(
                "POST",
                f"/Packages/Installed/{quote(self.PLUGIN_NAME)}",
                params={
                    "assemblyGuid": self.PLUGIN_GUID,
                    "repositoryUrl": self.PLUGIN_REPO_URL,
                },
            ).raise_for_status()
            _record("queue_install", True, "queued")
        except Exception as exc:
            result["error"] = f"could not queue install: {exc}"
            _record("queue_install", False, str(exc))
            return result

        # 4. Restart so the new plugin loads. Jellyfin's restart endpoint
        # responds 204 immediately and starts the shutdown asynchronously.
        try:
            self._request("POST", "/System/Restart").raise_for_status()
            _record("restart", True, "restart requested — wait ~30s then poll plugin status")
        except Exception as exc:
            # Restart is the only step that fails benignly — plugin is
            # downloaded, just won't load until next manual restart.
            _record("restart", False, str(exc))
            result["error"] = f"plugin queued but restart failed: {exc}"
            result["ok"] = True  # install succeeded even if restart didn't
            return result

        result["ok"] = True
        return result

    # ------------------------------------------------------------------
    # Per-library settings health check
    # ------------------------------------------------------------------
    #
    # Surface every Jellyfin library option that affects whether our
    # published trickplay actually shows up + whether new files get
    # auto-discovered. The Edit-Server modal renders these as a
    # checklist with a single "Fix all" button — users shouldn't need
    # to memorise four flag names spread across two Jellyfin admin
    # pages, especially when getting any one wrong silently breaks
    # the pipeline.

    # The recommended settings + rationale strings, defined once so
    # check + apply stay in sync. Order matters: the UI renders them
    # in this order so critical issues land at the top.
    _RECOMMENDED_SETTINGS: tuple[tuple[str, str, bool, str, str], ...] = (
        (
            "EnableTrickplayImageExtraction",
            "Trickplay enabled in Jellyfin",
            True,
            "critical",
            "Without this, Jellyfin ignores our published trickplay sheets entirely "
            "AND deletes them on the next library refresh. Must stay on.",
        ),
        (
            "SaveTrickplayWithMedia",
            "Look for trickplay next to the media file",
            True,
            "critical",
            "Tells Jellyfin to look in '<media>.trickplay/' (where this app writes) "
            "instead of '<config>/data/trickplay/' (which we never write to).",
        ),
        (
            "ExtractTrickplayImagesDuringLibraryScan",
            "Skip Jellyfin's own trickplay generation",
            False,
            "recommended",
            "When this app owns trickplay, Jellyfin's scan-time extraction is wasted CPU "
            "and produces duplicate output. Off = let this app do it; on = Jellyfin also "
            "burns CPU re-creating thumbnails we already published.",
        ),
        (
            "EnableRealtimeMonitor",
            "Auto-detect new files (real-time monitoring)",
            True,
            "recommended",
            "Without this, new files added by Sonarr/Radarr only get noticed on Jellyfin's "
            "next manual scan or a webhook nudge — the 'not in library yet' status hangs "
            "around longer than it needs to.",
        ),
    )

    def check_settings_health(self) -> list[HealthCheckIssue]:
        """Return a per-library audit of preview-relevant Jellyfin settings.

        Walks ``/Library/VirtualFolders`` once and emits one
        :class:`HealthCheckIssue` per (library, mis-set flag) pair.
        Empty list means all libraries are configured correctly.

        The flags inspected are documented in :data:`_RECOMMENDED_SETTINGS`
        — extending the audit means adding a tuple there; check + apply
        + UI explanation stay in sync automatically.
        """
        try:
            response = self._request("GET", "/Library/VirtualFolders")
            response.raise_for_status()
            folders = response.json()
        except Exception as exc:
            logger.warning(
                "Could not load Jellyfin library settings for health check on {!r}: {}. "
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
        """Flip every mis-set flag to its recommended value across all libraries.

        Args:
            flags: Restrict to the named flag list, or ``None`` for "every
                fixable issue currently surfaced by ``check_settings_health``".
                Names are the API-side flag keys (e.g. ``"EnableRealtimeMonitor"``).

        Returns dict keyed ``"<library_id>:<flag>"`` so the UI can render
        a per-row outcome ("✓ ok" or "✗ <error>"). Errors fetching the
        library list collapse to a single ``{"_global": "..."}`` entry.

        Implementation note: Jellyfin's ``/Library/VirtualFolders/LibraryOptions``
        is a wholesale replace, not a diff — we POST the full existing
        ``LibraryOptions`` dict back with just the targeted flags
        rewritten. Any field we omit reverts to its default, which has
        bitten previous one-off update attempts.
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
                    "Could not update Jellyfin library {} settings on server {!r}: {}",
                    lib_id,
                    self.name,
                    exc,
                )
                for flag in changed_flags:
                    results[f"{lib_id}:{flag}"] = f"error: {exc}"

        return results

    # The flag(s) that ``set_vendor_extraction`` flips for Jellyfin —
    # used by ``get_vendor_extraction_status`` to count per-library
    # state without forcing the apply path. EnableTrickplayImageExtraction
    # is intentionally NOT in this list: it MUST stay True regardless
    # (D38 — Jellyfin deletes our published trickplay when it's False).
    _VENDOR_EXTRACTION_FLAGS: tuple[tuple[str, bool], ...] = (
        ("ExtractTrickplayImagesDuringLibraryScan", False),
        ("SaveTrickplayWithMedia", True),
    )

    def get_vendor_extraction_status(self) -> dict[str, int]:
        """Audit per-library vendor-extraction state without writing.

        A library counts as ``stopped`` only when EVERY flag in
        :data:`_VENDOR_EXTRACTION_FLAGS` matches its recommended value;
        if any one is wrong it counts as ``extracting`` (the bulk apply
        would still touch this library).
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
            options = raw.get("LibraryOptions") or {}
            all_recommended = all(
                bool(options.get(flag, False)) == want for flag, want in self._VENDOR_EXTRACTION_FLAGS
            )
            if all_recommended:
                stopped += 1
            else:
                extracting += 1

        return {
            "extracting_count": extracting,
            "stopped_count": stopped,
            "skipped_count": 0,  # Jellyfin's per-library API never refuses, no skips
            "total": extracting + stopped,
        }

    def set_vendor_extraction(
        self,
        *,
        scan_extraction: bool,
        library_ids: list[str] | None = None,
    ) -> dict[str, str]:
        """Toggle Jellyfin's scan-time trickplay generation per library.

        Used by the "Vendor-side preview generation" panel on the Edit
        Server modal. When the user lets THIS app handle preview
        generation, Jellyfin's own scan-time extraction is wasted CPU.

        Three flags + one scheduled task all need handling — Jellyfin's
        trickplay subsystem is gated by several knobs and missing any
        one re-introduces the spike:

        1. ``EnableTrickplayImageExtraction`` — KEEP True. This is the
           detection / serving gate AND a destructive prune flag — when
           False, ``RefreshTrickplayDataAsync`` ``Directory.Delete``s our
           saved-with-media output on the next refresh.
        2. ``ExtractTrickplayImagesDuringLibraryScan`` — set to
           ``scan_extraction``. ``TrickplayProvider.FetchInternal``
           early-returns when this is False, skipping per-item scan
           extraction.
        3. ``SaveTrickplayWithMedia`` — set True when ``scan_extraction``
           is False. Without this Jellyfin looks for trickplay under
           ``<config>/data/trickplay/<id[..2]>/<id>/...`` (where we never
           write); with it, Jellyfin looks under
           ``<media_dir>/<basename>.trickplay/<width> - <tileW>x<tileH>/``
           (where ``JellyfinTrickplayAdapter`` does write).

        The "Refresh Trickplay Images" daily scheduled task is left at
        its default 3am trigger. We tried clearing it (D38 first cut)
        but that produced a silent failure: ``RefreshTrickplayDataAsync``
        is THE import path for our published trickplay, so without the
        daily run our files sit on disk and Jellyfin's web client gets
        404 on the trickplay HLS endpoint. With the task running, it
        imports our files cheaply for covered items (no ffmpeg) and
        only generates for items we haven't processed yet — a bounded
        CPU spike that shrinks as the library is covered. The user can
        still strip the trigger manually via Jellyfin admin if they
        want a no-ffmpeg-ever guarantee.

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
        target_ids = set(library_ids) if library_ids else None
        for raw in folders:
            if not isinstance(raw, dict):
                continue
            lib_id = str(raw.get("ItemId") or raw.get("Id") or raw.get("Name") or "")
            if target_ids is not None and lib_id not in target_ids:
                continue

            options = dict(raw.get("LibraryOptions") or {})
            # Detection always on so our published trickplay is actually used.
            options["EnableTrickplayImageExtraction"] = True
            options["ExtractTrickplayImagesDuringLibraryScan"] = bool(scan_extraction)
            # When we own generation, point Jellyfin at the media-relative
            # path our adapter writes to. When the user wants Jellyfin to
            # own generation again, leave the flag alone — they may have
            # set it intentionally either way.
            if not scan_extraction:
                options["SaveTrickplayWithMedia"] = True

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
                    "Could not update Jellyfin library {} extraction on server {!r}: {}",
                    lib_id,
                    self.name,
                    exc,
                )
                results[lib_id] = f"error: {exc}"
        return results

    def parse_webhook(
        self,
        payload: dict[str, Any] | bytes,
        headers: dict[str, str],
    ) -> WebhookEvent | None:
        """Normalise a jellyfin-plugin-webhook payload to a :class:`WebhookEvent`.

        The plugin's stock ``ItemAdded`` template emits:
        ``{"NotificationType": "ItemAdded", "ItemId": "...", "ItemType": "Episode", ...}``.
        We only act on ``ItemAdded`` (and Emby-flavoured ``library.new``
        if a user has copied an Emby template); anything else returns
        ``None``.
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

        event_type = str(data.get("NotificationType") or data.get("Event") or "")
        if event_type.lower() not in {"itemadded", "library.new"}:
            return None

        item_id = str(data.get("ItemId") or data.get("Id") or "")
        # The Jellyfin webhook plugin's "Generic Destination" template
        # commonly includes ``{{ItemPath}}`` (or ``ItemPath`` / ``Path``
        # via custom templates). When present, capturing it here lets the
        # dispatcher skip an extra reverse-lookup roundtrip per webhook.
        # Audit fix — was being silently dropped.
        item_path = str(data.get("ItemPath") or data.get("Path") or data.get("path") or "").strip() or None

        return WebhookEvent(
            event_type=event_type,
            item_id=item_id or None,
            remote_path=item_path,
            raw=data,
        )
