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

import requests
from loguru import logger

from ._embyish import EmbyApiClient
from .base import FlagTarget, HealthCheckIssue, ServerType, WebhookEvent

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
        # Per-instance cache of Media Preview Bridge plugin presence —
        # read by the dispatcher to decide whether an item-id lookup is
        # worth paying for (plugin ⇒ ~200ms, no plugin ⇒ ~30s cold).
        # Populated by ``check_plugin_installed`` and refreshed after
        # ``install_plugin``. ``None`` means "not probed yet".
        self._media_preview_bridge_installed: bool | None = None

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
            logger.info(
                "[{}] Triggered partial scan: {}",
                self.name,
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

    def _trigger_path_deleted(self, server_view_path: str) -> None:
        """Tell Jellyfin a previously-imported file is gone.

        Same ``/Library/Media/Updated`` endpoint as
        :meth:`_trigger_path_refresh`, but with ``UpdateType:"Deleted"``
        so Jellyfin drops the stale library row immediately. Used after
        Radarr/Sonarr upgrade webhooks where the payload's
        ``deletedFiles[]`` lists the prior release that was replaced —
        without this, Jellyfin's library item lingers on the old path
        until its filesystem monitor or the 3 AM scheduled scan
        notices the deletion.

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
                logger.info(
                    "[{}] Registered trickplay via Media Preview Bridge plugin: item {}",
                    self.name,
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
            logger.info(
                "[{}] Triggered item refresh: {}",
                self.name,
                item_id,
            )
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
            logger.info(
                "[{}] Triggered full library refresh (path-based nudge unavailable)",
                self.name,
            )
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
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            # Server is unreachable / overloaded. Falling through to
            # the base resolver hits the SAME server with the SAME
            # symptoms — wasted second 30s timeout. Job baf4f9cc
            # (Jersey Shore Family Vacation, 2026-05-06 08:36-37)
            # showed exactly this 30s × 2 = 59.4s pattern across all 3
            # files when JellyTest was contention-locked by its own
            # post-Sonarr-import scan.
            #
            # Return None and let the slow-backoff retry queue try
            # again 30s later when the server should be idle. Recall
            # is preserved by the retry — losing one webhook fire to
            # an overloaded server is far cheaper than burning the
            # second 30s on every per-file dispatch.
            logger.warning(
                "Media Preview Bridge ResolvePath unreachable for {!r} ({}: {}) — "
                "skipping base-resolver fallback to avoid a second timeout against the "
                "same overloaded server. Slow-backoff retry queue will pick this up.",
                remote_path,
                type(exc).__name__,
                exc,
            )
            return None
        except Exception as exc:
            # Any other exception (HTTPError, JSON decode errors, etc.)
            # is plugin-specific and the base resolver may still find
            # the file via Pass 0/1/2. Fall through.
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
            self._media_preview_bridge_installed = False
            return {"installed": False, "version": "", "error": f"{type(exc).__name__}: {exc}"[:200]}
        if response.status_code != 200:
            self._media_preview_bridge_installed = False
            return {"installed": False, "version": "", "error": f"HTTP {response.status_code}"}
        try:
            payload = response.json()
            installed = bool(payload.get("ok"))
            self._media_preview_bridge_installed = installed
            return {
                "installed": installed,
                "version": str(payload.get("version") or ""),
                "error": "",
            }
        except (ValueError, AttributeError) as exc:
            self._media_preview_bridge_installed = False
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
            # Cache is intentionally NOT flipped here — the plugin isn't
            # live until the restart completes. The caller polls
            # check_plugin_installed() post-restart which will refresh it.
            return result

        result["ok"] = True
        # Same reasoning: caller polls check_plugin_installed() after the
        # restart finishes. Leaving the cache at its prior value avoids a
        # window where the dispatcher thinks the plugin is live but the
        # server is mid-restart and returning 503.
        return result

    def uninstall_plugin(self) -> dict[str, Any]:
        """Reverse of :meth:`install_plugin`: delete the plugin, restart Jellyfin.

        Used by the "Uninstall plugin" row on the Previews readiness
        card. Falls the server back from Mode A (instant activation) to
        Mode B (scan-nudge / 3 AM task adoption) — published tiles stay
        on disk and are re-discovered via the normal trickplay import
        path after Jellyfin restarts, so the change isn't data-destructive.

        Steps, all best-effort with structured errors so the UI can
        surface progress:

        1. ``DELETE /Plugins/{PLUGIN_GUID}`` — remove the installed
           plugin by its assembly GUID. **404 is treated as success** —
           "already not installed" is the desired end state.
           (``/Packages/{guid}`` is the wrong endpoint — Jellyfin
           returns 405 Method Not Allowed; ``/Packages`` is the
           install-catalogue API.)
        2. ``POST /System/Restart`` — Jellyfin unloads plugins on
           startup, so a restart is required for the uninstall to take
           effect visibly.

        Repo URL is deliberately LEFT in place. Users may want to
        re-install later without re-adding the manifest, and leaving
        the repo entry does nothing harmful on its own (Jellyfin only
        downloads plugins when explicitly asked).

        Caller should poll :meth:`check_plugin_installed` after this
        returns to know when the restart finished (Jellyfin restarts
        asynchronously; takes ~10–30s on a typical install).
        """
        result: dict[str, Any] = {"steps": [], "ok": False, "error": ""}

        def _record(step: str, ok: bool, detail: str = "") -> None:
            result["steps"].append({"step": step, "ok": ok, "detail": detail})

        try:
            response = self._request("DELETE", f"/Plugins/{self.PLUGIN_GUID}")
        except Exception as exc:
            result["error"] = f"could not send uninstall request: {exc}"
            _record("uninstall_package", False, str(exc))
            return result

        if response.status_code in (200, 204):
            _record("uninstall_package", True, "removed")
        elif response.status_code == 404:
            # Already gone — this is the desired end state, treat as success.
            _record("uninstall_package", True, "already not installed")
        else:
            detail = f"HTTP {response.status_code}"
            try:
                body = (response.text or "")[:200]
                if body:
                    detail = f"{detail}: {body}"
            except Exception:
                pass
            result["error"] = f"uninstall failed: {detail}"
            _record("uninstall_package", False, detail)
            return result

        try:
            self._request("POST", "/System/Restart").raise_for_status()
            _record("restart", True, "restart requested — wait ~30s then poll plugin status")
        except Exception as exc:
            # Restart is the only step that fails benignly — plugin is
            # removed, just won't unload until next manual restart.
            _record("restart", False, str(exc))
            result["error"] = f"plugin removed but restart failed: {exc}"
            result["ok"] = True  # uninstall succeeded even if restart didn't
            return result

        result["ok"] = True
        # Caller polls check_plugin_installed() after the restart
        # finishes — same pattern as install_plugin.
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

    def _recommended_settings(self) -> tuple[tuple[str, str, bool, str, str], ...]:
        """Settings + rationale, parameterised by plugin presence.

        ``ExtractTrickplayImagesDuringLibraryScan`` is the only flag
        whose recommendation depends on plugin state:

        * Plugin installed (Mode A) — recommend ``False``. The plugin
          activates our tiles instantly via ``SaveTrickplayInfo``;
          Jellyfin's scan-time extraction is wasted CPU.
        * Plugin absent (Mode B) — recommend ``True``. Without the
          plugin we rely on ``/Library/Media/Updated`` triggering
          ``TrickplayProvider`` for adoption, and that path is gated
          by this flag (``TrickplayProvider.cs`` L94-108).

        All other flags are identical in both modes. Order matters —
        the UI renders them top-to-bottom and critical issues should
        land first.
        """
        plugin = getattr(self, "_media_preview_bridge_installed", None)
        # When plugin state is unknown (never probed), assume "no
        # plugin" for settings recommendations — safe default, Mode B
        # works without plugin and survives a later plugin install
        # without any re-fix needed (extra flag = redundant, not broken).
        plugin_installed = bool(plugin)
        scan_ext_recommended = not plugin_installed
        scan_ext_severity = "recommended" if plugin_installed else "critical"
        scan_ext_rationale = (
            "With the Media Preview Bridge plugin installed, this app registers "
            "previews instantly and Jellyfin's scan-time extraction is just wasted "
            "CPU. Recommended: off."
            if plugin_installed
            else "Without the plugin, Jellyfin needs this flag ON to trigger the "
            "'import existing tiles' path — which is how our previews get adopted "
            "when they land on disk. Off = previews only activate at 3 AM "
            "(Jellyfin's daily scheduled task). Strongly recommended: on."
        )

        return (
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
                "Jellyfin scan-time extraction",
                scan_ext_recommended,
                scan_ext_severity,
                scan_ext_rationale,
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

    # Back-compat alias for test code and callers that read the tuple
    # directly. New code should prefer ``_recommended_settings()`` which
    # returns the plugin-aware set.
    @property
    def _RECOMMENDED_SETTINGS(self) -> tuple[tuple[str, str, bool, str, str], ...]:  # noqa: N802
        return self._recommended_settings()

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
        recommended = self._recommended_settings()
        for raw in folders:
            if not isinstance(raw, dict):
                continue
            lib_id = str(raw.get("ItemId") or raw.get("Id") or raw.get("Name") or "")
            lib_name = str(raw.get("Name") or "")
            options = raw.get("LibraryOptions") or {}
            for flag, label, want, severity, rationale in recommended:
                current = bool(options.get(flag, False))
                if current == want:
                    continue
                issues.append(
                    HealthCheckIssue(
                        library_id=lib_id,
                        library_name=lib_name,
                        flag=flag,
                        label=label,
                        rationale=rationale,
                        current=current,
                        recommended=want,
                        severity=severity,
                        fixable=True,
                    )
                )
        return issues

    # ------------------------------------------------------------------
    # UX metadata for per-check toggles + tooltips
    # ------------------------------------------------------------------
    #
    # The Previews readiness card's per-row ⓘ tooltips + docs anchors
    # + enable/disable confirm dialogs are all driven by this table.
    # Keeping the metadata next to the flag definition means the API
    # payload, the tooltip, the doc anchor and the destructive-confirm
    # blob can never drift apart.

    # Destructive-confirm payloads. Each is rendered into the frontend's
    # confirm modal verbatim — phrase + body come from the server so
    # unit tests can verify the right copy ships without scraping the JS.
    # The (flag, False)→phrase map below is also consulted by the
    # `/health-check/apply` route to enforce the type-to-confirm guard
    # SERVER-SIDE — the UI modal is UX gloss, not a security boundary,
    # so a curl/bookmarklet/XHR replay that skips the modal still has
    # to carry the required phrase in the body or the route returns 400.
    _CONFIRM_ENABLE_TRICKPLAY_OFF: dict[str, str] = {
        "kind": "type",
        "phrase": "disable trickplay",
        "body": (
            "Jellyfin will DELETE the published .trickplay/ directory on its "
            "next library refresh — every preview tile this app has generated "
            "will be gone. You will need to re-run the generator to restore "
            "them. Type the phrase below to confirm."
        ),
    }
    _CONFIRM_SAVE_TRICKPLAY_OFF: dict[str, str] = {
        "kind": "button",
        "phrase": "",
        "body": (
            "Jellyfin will stop looking for previews next to your media files. "
            "Published tiles will stay on disk but become invisible to Jellyfin "
            "until this is re-enabled."
        ),
    }
    _CONFIRM_SCAN_EXTRACTION_OFF: dict[str, str] = {
        "kind": "button",
        "phrase": "",
        "body": (
            "Without this AND without the Media Preview Bridge plugin, Jellyfin "
            "only adopts our previews at 3 AM (its daily scheduled task). "
            "Newly added files will have no scrubbing preview until that runs."
        ),
    }
    _CONFIRM_UNINSTALL_PLUGIN: dict[str, str] = {
        "kind": "button",
        "phrase": "",
        "body": (
            "Removes the Media Preview Bridge plugin and restarts Jellyfin (~30 seconds). "
            "After restart, activation switches from instant to on-next-scan: newly "
            "generated previews will wait for Jellyfin's next library scan or the 3 AM "
            "daily refresh before appearing in the player. "
            "Already-published tile files stay on disk untouched — this change is "
            "reversible by re-installing the plugin any time."
        ),
    }

    # Per-flag metadata — structured so the ⓘ popover, the
    # enable-confirm dialog, and the disable-confirm dialog all read
    # from the same source of truth. Keys:
    #
    #   check_id / docs_anchor — stable identifiers for the API + docs.
    #   tooltip                — short one-line label (shown inline beside
    #                            the row; NOT the popover body).
    #   explanation            — multi-paragraph rich HTML for the ⓘ
    #                            popover: what / why / what-happens-if-you
    #                            -flip-it. Covers BOTH directions. Rendered
    #                            via innerHTML (only server-controlled
    #                            literals land here — no user input).
    #   enable_body            — PLAIN-TEXT prose for the "Enable" confirm
    #                            dialog (no HTML tags — rendered via
    #                            textContent). Explains what turning the
    #                            flag ON does and what it costs.
    #   disable_body           — same for "Disable", PLAIN TEXT. Destructive
    #                            cases cite the concrete data-loss risk.
    #   disable_kind           — "type" for data-destructive flips
    #                            (requires the user to type the exact
    #                            phrase), "button" otherwise. Defaults
    #                            to "button".
    #   disable_phrase         — required typed phrase when
    #                            disable_kind == "type". Ignored otherwise.
    _FLAG_METADATA: dict[str, dict[str, Any]] = {
        "EnableTrickplayImageExtraction": {
            "check_id": "enable_trickplay",
            "docs_anchor": "enable-trickplay",
            "tooltip": "Jellyfin's master trickplay switch",
            "explanation": (
                "<p><strong>What it does:</strong> this is Jellyfin's master trickplay gate. "
                "When on, Jellyfin recognises trickplay tile directories (the scrubbing-preview "
                "images this app writes to <code>&lt;media&gt;.trickplay/</code>) and serves them "
                "to the web/mobile players.</p>"
                "<p><strong>Why we recommend on:</strong> without this flag Jellyfin completely "
                "ignores our published tiles — AND will DELETE the <code>.trickplay/</code> "
                "directory on the next library refresh because its internal logic treats the "
                "files as orphaned. That's data loss: you'd need to re-run the generator to "
                "restore previews for every file in the library.</p>"
                "<p><strong>What happens if you disable it:</strong> Jellyfin stops serving "
                "scrubbing previews across all players, and on the next scheduled library "
                "refresh (or a manual Refresh Metadata) every <code>.trickplay/</code> directory "
                "on disk gets deleted. Re-enabling later doesn't bring those files back — you "
                "have to regenerate them.</p>"
            ),
            "enable_body": (
                "Turns ON Jellyfin's master trickplay switch. Published preview tiles become "
                "visible to clients and are no longer at risk of being deleted by Jellyfin's "
                "next refresh cycle. This is the recommended state."
            ),
            "disable_body": (
                "Jellyfin will DELETE the published .trickplay/ directory on its next library "
                "refresh — every preview tile this app has generated will be gone. "
                "You will need to re-run the generator to restore them. "
                "Type the phrase below to confirm you understand this is data-destructive."
            ),
            "disable_kind": "type",
            "disable_phrase": "disable trickplay",
        },
        "SaveTrickplayWithMedia": {
            "check_id": "save_trickplay_with_media",
            "docs_anchor": "save-trickplay-with-media",
            "tooltip": "Look for trickplay next to the media file",
            "explanation": (
                "<p><strong>What it does:</strong> tells Jellyfin where on disk to look for "
                "trickplay tiles. On = next to the video file "
                "(<code>&lt;media&gt;.trickplay/</code>, which is where this app writes). "
                "Off = inside Jellyfin's config directory "
                "(<code>&lt;config&gt;/data/trickplay/</code>, which this app never writes to).</p>"
                "<p><strong>Why we recommend on:</strong> off effectively hides every preview "
                "this app has ever published — the files stay on disk but Jellyfin can't find "
                "them. Re-enabling makes them visible again without regenerating, so this "
                "isn't data-destructive, just invisibility.</p>"
                "<p><strong>What happens if you disable it:</strong> every scrubbing preview "
                "this app has published becomes invisible to Jellyfin clients. Existing "
                "<code>.trickplay/</code> files stay on disk untouched, so the change is "
                "reversible by simply re-enabling.</p>"
            ),
            "enable_body": (
                "Jellyfin will look for trickplay next to each media file (where this app "
                "writes), so published previews become visible again. Safe and reversible."
            ),
            "disable_body": (
                "Jellyfin will stop looking for previews next to your media files. Published "
                "tiles stay on disk (nothing is deleted) but become invisible to Jellyfin "
                "clients until this is re-enabled."
            ),
        },
        "ExtractTrickplayImagesDuringLibraryScan": {
            "check_id": "scan_extraction",
            "docs_anchor": "scan-extraction",
            "tooltip": "Jellyfin scan-time trickplay generation",
            # The recommendation flips with plugin state — explanation text
            # covers BOTH modes so the popover is informative in either state.
            "explanation": (
                "<p><strong>What it does:</strong> controls whether Jellyfin runs its own "
                "trickplay generation during library scans. When on, Jellyfin will scan every "
                "video file and generate preview tiles itself if none exist.</p>"
                "<p><strong>Why we recommend it depends on the plugin:</strong></p>"
                "<ul>"
                "<li><strong>With the Media Preview Bridge plugin installed:</strong> "
                "recommend OFF. The plugin registers our published tiles instantly via a direct "
                "API call, so Jellyfin running its own extraction on top is just wasted CPU "
                "and produces duplicate output.</li>"
                "<li><strong>Without the plugin:</strong> recommend ON. Jellyfin's "
                "scan-time code is the ADOPTION path — when it scans a file with existing "
                "<code>.trickplay/</code> tiles next to it, it imports them into its database "
                "(no ffmpeg, instant). With this flag off AND no plugin, adoption stalls until "
                "the 3 AM daily 'Refresh Trickplay Images' task runs.</li>"
                "</ul>"
                "<p><strong>What happens if you deviate:</strong> extra CPU spikes during scans "
                "(when on with plugin), or delayed adoption until the next daily task (when "
                "off without plugin). Never data-destructive.</p>"
            ),
            "enable_body": (
                "Jellyfin will run its own trickplay generation during library scans. "
                "Without the Media Preview Bridge plugin this is how our published tiles get "
                "adopted — keep it ON. With the plugin installed this just duplicates work "
                "and wastes CPU during scans."
            ),
            "disable_body_no_plugin": (
                "Without the Media Preview Bridge plugin AND without this flag, Jellyfin only "
                "adopts our previews at 3 AM (its daily scheduled task). New files will have "
                "no scrubbing preview until then. Nothing is deleted."
            ),
            "disable_body_with_plugin": (
                "With the plugin installed, Jellyfin's scan-time extraction is wasted CPU. "
                "Disabling stops the duplicate work — adoption still happens instantly via "
                "the plugin. Safe."
            ),
        },
        "EnableRealtimeMonitor": {
            "check_id": "realtime_monitor",
            "docs_anchor": "realtime-monitor",
            "tooltip": "Auto-detect new files (real-time monitoring)",
            "explanation": (
                "<p><strong>What it does:</strong> tells Jellyfin to watch the library's folder "
                "tree for filesystem changes (new files, moves, renames) and pick them up "
                "immediately instead of waiting for the next scheduled scan.</p>"
                "<p><strong>Why we recommend on:</strong> Sonarr/Radarr imports a file, Jellyfin "
                "notices within seconds, this app's webhook gets fired, preview gets generated "
                "and published — total latency measured in seconds. With this off, the flow "
                "stalls on Jellyfin's next manual scan or a webhook nudge from this app (we "
                "send them, but they can be missed on network hiccups).</p>"
                "<p><strong>What happens if you disable it:</strong> new files don't show up in "
                "Jellyfin (or this app's preview pipeline) until someone triggers a scan. "
                "Non-destructive and reversible — just slower.</p>"
            ),
            "enable_body": (
                "Jellyfin will watch the library folders and auto-detect new files instantly. "
                "This is the recommended state for fast preview generation after Sonarr/Radarr "
                "imports."
            ),
            "disable_body": (
                "Jellyfin will stop watching the filesystem for new files. New episodes/movies "
                "imported by Sonarr/Radarr won't show up in Jellyfin — or kick off preview "
                "generation — until a manual scan runs. Nothing is deleted; re-enable any time."
            ),
        },
    }

    @classmethod
    def destructive_confirm_phrase(cls, flag: str, value: Any) -> str | None:
        """Return the typed-phrase required to set ``flag`` to ``value``, or None.

        Used by :meth:`_flag_actions` (to emit the confirm payload for
        the UI) AND by the ``/health-check/apply`` route (to enforce
        the same guardrail server-side — a curl/bookmarklet that skips
        the confirm modal still has to carry the phrase in the body or
        the request 400s). The UI modal is UX gloss; this map is the
        single source of truth for "which flag flips need a typed ack".

        Currently only ``EnableTrickplayImageExtraction -> False`` is
        guarded this way because it's the one data-destructive case in
        the Jellyfin surface (other disable paths are reversible).
        """
        if flag == "EnableTrickplayImageExtraction" and value is False:
            return cls._CONFIRM_ENABLE_TRICKPLAY_OFF["phrase"]
        return None

    def _flag_actions(self, flag: str, current: bool, plugin_installed: bool) -> dict[str, Any]:
        """Build the ``actions`` blob for a flag row.

        Each action carries ``{action, args, confirm}`` — rendered into
        the server payload verbatim so the UI's dispatcher is
        data-driven (no frontend heuristics for which toggles are safe).

        Every enable/disable carries a confirm blob so users always see
        an explanation of what they're about to do before the POST
        fires. ``kind`` is "button" for reversible flips and "type" for
        data-destructive flips (only ``EnableTrickplayImageExtraction →
        false`` today).

        The plugin-state-dependent disable copy for
        ``ExtractTrickplayImagesDuringLibraryScan`` is resolved here
        (the disable is less urgent with the plugin installed).
        """
        meta = self._FLAG_METADATA.get(flag, {})

        disable_body = meta.get("disable_body") or ""
        # Plugin-state-dependent disable body for scan-extraction.
        if flag == "ExtractTrickplayImagesDuringLibraryScan":
            if plugin_installed:
                disable_body = meta.get("disable_body_with_plugin") or disable_body
            else:
                disable_body = meta.get("disable_body_no_plugin") or disable_body

        disable_kind = meta.get("disable_kind", "button")
        disable_phrase = meta.get("disable_phrase", "")

        actions: dict[str, Any] = {}
        # Only expose the action that would CHANGE state — offering an
        # "enable" toggle on an already-enabled flag is UX noise.
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
                    "kind": disable_kind,
                    "phrase": disable_phrase,
                    "body": disable_body,
                },
            }
        return actions

    def apply_flag_values(self, targets: list[FlagTarget]) -> dict[str, str]:
        """Set each ``(flag, value)`` pair to its explicit value across libraries.

        Jellyfin's ``/Library/VirtualFolders/LibraryOptions`` is a
        wholesale replace — we POST the full existing ``LibraryOptions``
        dict back with just the targeted flags rewritten. Fields we
        omit revert to their defaults (same pattern as
        :meth:`apply_recommended_settings`).

        Args:
            targets: List of ``{flag, value, library_ids}`` rows. When
                ``library_ids`` is ``None`` or missing, the flag is
                applied to every library; otherwise only the listed
                ids are touched.

        Returns:
            Dict keyed ``"<library_id>:<flag>"`` so the UI can render a
            per-row outcome.
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

        # Index targets by flag for quick per-library application. Each
        # (flag, library_id) pair maps to exactly one desired value;
        # later entries overwrite earlier ones for the same (flag,
        # lib_id) key.
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
            lib_id = str(raw.get("ItemId") or raw.get("Id") or raw.get("Name") or "")
            options = dict(raw.get("LibraryOptions") or {})

            changed: list[str] = []
            for flag, target_rows in per_flag.items():
                # Pick the most-specific matching row for this lib:
                # an entry with matching library_ids wins over a
                # wildcard (library_ids=None) entry.
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
                # Normalise booleans so "true"/"True" etc. compare right.
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
                    "Could not update Jellyfin library {} settings on server {!r}: {}",
                    lib_id,
                    self.name,
                    exc,
                )
                for flag in changed:
                    results[f"{lib_id}:{flag}"] = f"error: {exc}"

        return results

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
        recommended = self._recommended_settings()

        for raw in folders:
            if not isinstance(raw, dict):
                continue
            lib_id = str(raw.get("ItemId") or raw.get("Id") or raw.get("Name") or "")
            options = dict(raw.get("LibraryOptions") or {})

            changed_flags: list[str] = []
            for flag, _label, want, _sev, _rationale in recommended:
                if target_flags is not None and flag not in target_flags:
                    continue
                if bool(options.get(flag, False)) == want:
                    continue
                options[flag] = want
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

    # ------------------------------------------------------------------
    # Unified "Previews readiness" probe + auto-fix
    # ------------------------------------------------------------------

    def _adapter_geometry(self) -> dict[str, int]:
        """Our Jellyfin trickplay adapter's geometry (read once).

        Width + frame_interval come from this server's ``output`` settings
        (shared shape with the plugin-registration path at line 144 above
        so the two views can never disagree). Tile dimensions are the
        adapter's hardcoded 10x10 constants.
        """
        output = (self._config.output or {}) if getattr(self, "_config", None) else {}
        return {
            "width": int(output.get("width") or 320),
            "tile_w": 10,
            "tile_h": 10,
            "interval_ms": int(output.get("frame_interval") or 10) * 1000,
        }

    def fetch_trickplay_options(self) -> dict[str, Any] | None:
        """Return the server-wide ``TrickplayOptions`` sub-dict.

        Jellyfin 10.11 stores trickplay options as a NESTED property
        inside ``/System/Configuration`` (``.TrickplayOptions``) —
        there's no sub-path for it. Probed live against Jellyfin
        10.11.8: ``GET /System/Configuration/trickplay`` returns 404,
        ``GET /System/Configuration`` returns the whole config dict
        with ``TrickplayOptions`` as one field.

        Returns ``None`` when the endpoint fails (older Jellyfin,
        permission denied, server unreachable). Callers should treat
        ``None`` as "can't probe, render the row as unknown" rather
        than as a correctness claim.
        """
        try:
            response = self._request("GET", "/System/Configuration")
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.debug(
                "Could not fetch /System/Configuration on {!r}: {}",
                self.name,
                exc,
            )
            return None
        if not isinstance(data, dict):
            return None
        tp = data.get("TrickplayOptions")
        return tp if isinstance(tp, dict) else None

    def sync_trickplay_options(self) -> dict[str, Any]:
        """Ensure server-wide TrickplayOptions matches our adapter's geometry.

        Jellyfin's adoption code
        (``TrickplayManager.RefreshTrickplayDataInternal`` L256-261)
        synthesises the ``TrickplayInfo`` DB row from server-wide
        ``TrickplayOptions`` verbatim — not measured from tiles. A
        mismatch (e.g. server ``TileWidth=8`` vs our adapter's 10)
        silently breaks client rendering: adoption "succeeds" but the
        scrubber pulls the wrong pixel range per tile.

        Fetch-merge-POST pattern. ``/System/Configuration`` is a
        wholesale replace (verified — Jellyfin stores the full config
        dict and replaces it atomically). We:

        1. GET the complete ``/System/Configuration`` dict.
        2. Rewrite ONLY ``TrickplayOptions.TileWidth``, ``TileHeight``, ``Interval``.
        3. Extend ``WidthResolutions`` to include our adapter's width
           (never replace — a user wanting additional widths keeps them).
        4. POST the complete mutated dict back.

        Returns ``{"ok": bool, "error": str, "before": {...}, "after": {...}}``
        so the UI can show what changed. ``before`` / ``after`` are the
        ``TrickplayOptions`` sub-dicts, not the whole config.
        """
        geometry = self._adapter_geometry()
        try:
            response = self._request("GET", "/System/Configuration")
            response.raise_for_status()
            full_config = response.json()
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Could not read /System/Configuration: {exc}",
                "before": None,
                "after": None,
            }
        if not isinstance(full_config, dict):
            return {
                "ok": False,
                "error": "Unexpected /System/Configuration response shape",
                "before": None,
                "after": None,
            }

        before_tp = full_config.get("TrickplayOptions")
        if not isinstance(before_tp, dict):
            return {
                "ok": False,
                "error": "Server config has no TrickplayOptions block",
                "before": None,
                "after": None,
            }

        after_tp = dict(before_tp)
        after_tp["TileWidth"] = geometry["tile_w"]
        after_tp["TileHeight"] = geometry["tile_h"]
        after_tp["Interval"] = geometry["interval_ms"]

        existing_widths = list(after_tp.get("WidthResolutions") or [])
        if geometry["width"] not in existing_widths:
            existing_widths.append(geometry["width"])
            # Jellyfin renders resolutions in the order stored — keep
            # ours first so the default player width matches our tiles.
            existing_widths.insert(0, existing_widths.pop())
        after_tp["WidthResolutions"] = existing_widths

        mutated_config = dict(full_config)
        mutated_config["TrickplayOptions"] = after_tp

        try:
            update = self._request("POST", "/System/Configuration", json_body=mutated_config)
            update.raise_for_status()
        except Exception as exc:
            return {"ok": False, "error": str(exc), "before": before_tp, "after": after_tp}
        return {"ok": True, "error": "", "before": before_tp, "after": after_tp}

    def _check_trickplay_options(self) -> dict[str, Any]:
        """Compare server-wide ``TrickplayOptions`` with our adapter geometry."""
        ours = self._adapter_geometry()
        server = self.fetch_trickplay_options()
        if server is None:
            return {
                "ok": False,
                "server": None,
                "ours": ours,
                "fix_kind": "set_trickplay_options",
                "reason": "Could not read server TrickplayOptions",
            }
        mismatches: list[str] = []
        if int(server.get("TileWidth") or 0) != ours["tile_w"]:
            mismatches.append(f"TileWidth={server.get('TileWidth')!r} (ours={ours['tile_w']})")
        if int(server.get("TileHeight") or 0) != ours["tile_h"]:
            mismatches.append(f"TileHeight={server.get('TileHeight')!r} (ours={ours['tile_h']})")
        if int(server.get("Interval") or 0) != ours["interval_ms"]:
            mismatches.append(f"Interval={server.get('Interval')!r}ms (ours={ours['interval_ms']}ms)")
        widths = [int(w) for w in (server.get("WidthResolutions") or [])]
        if ours["width"] not in widths:
            mismatches.append(f"WidthResolutions missing {ours['width']} (has {widths})")
        return {
            "ok": not mismatches,
            "server": server,
            "ours": ours,
            "fix_kind": "set_trickplay_options" if mismatches else None,
            "reason": "; ".join(mismatches) if mismatches else "",
        }

    def _parse_version_tuple(self, version: str) -> tuple[int, ...]:
        """Parse ``"10.11.2"`` → ``(10, 11, 2)``; defensive against garbage."""
        parts: list[int] = []
        for raw in str(version).split("."):
            try:
                parts.append(int(raw.split("-")[0].split("+")[0]))
            except (ValueError, TypeError):
                break
            if len(parts) >= 3:
                break
        return tuple(parts)

    def previews_readiness(self) -> dict[str, Any]:
        """Unified readiness payload for the Previews readiness card.

        Walks the four independent probes (connection implicit, version,
        plugin, per-library flags, server-wide TrickplayOptions) and
        emits them as ``sections`` the frontend renders in order. Each
        check carries explicit enable/disable ``actions`` blobs so the
        UI toggles are data-driven.

        Returns the envelope documented on
        :meth:`MediaServer.previews_readiness`. Key sections Jellyfin
        emits: ``connection`` (implicit — via version probe), ``version``,
        ``plugin``, ``library_settings``, ``server_options``,
        ``vendor_extraction``. (Path mappings are a Plex-only concept —
        Jellyfin's webhook plugin reports paths the way Jellyfin sees
        them, so no mapping section is emitted here.)
        """
        # Probe plugin first — all downstream checks depend on its state.
        plugin = self.check_plugin_installed()
        plugin_installed = bool(plugin.get("installed"))

        # Fetch library options up-front so the plugin section can decide
        # whether plugin absence is a real break (any library in Mode A —
        # ``ExtractTrickplayImagesDuringLibraryScan = false`` — requires
        # the plugin to adopt our tiles; without it, scrubbing previews
        # never render). The library-settings section below reuses the
        # same ``folders`` payload.
        try:
            response = self._request("GET", "/Library/VirtualFolders")
            response.raise_for_status()
            folders = response.json()
        except Exception as exc:
            logger.debug("Library flags probe failed for {!r}: {}", self.name, exc)
            folders = []

        mode_a_library_names: list[str] = []
        if isinstance(folders, list):
            for raw in folders:
                if not isinstance(raw, dict):
                    continue
                options = raw.get("LibraryOptions") or {}
                if options.get("ExtractTrickplayImagesDuringLibraryScan") is False:
                    mode_a_library_names.append(str(raw.get("Name") or "").strip() or "library")
        plugin_required = bool(mode_a_library_names)

        sections: list[dict[str, Any]] = []

        # --- Connection + version (combined into one section) ---------
        version_value = ""
        version_ok = True
        version_reason = ""
        connection_ok = True
        connection_reason = ""
        try:
            response = self._request("GET", "/System/Info")
            response.raise_for_status()
            data = response.json() or {}
            version_value = str(data.get("Version") or "") or ""
            parsed = self._parse_version_tuple(version_value)
            if parsed and parsed < (10, 10):
                version_ok = False
                version_reason = (
                    f"Jellyfin {version_value} is pre-10.10 — SaveTrickplayWithMedia "
                    "isn't supported. Upgrade to 10.10+ for native adoption."
                )
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
                        "label": "Jellyfin reachable",
                        "docs_anchor": "connection",
                        "tooltip": "Server is reachable and responds to API calls",
                        "explanation": (
                            "<p><strong>What it checks:</strong> this app sent a GET to "
                            "<code>/System/Info</code> on the configured Jellyfin URL and got back "
                            "a successful JSON response.</p>"
                            "<p><strong>Why it matters:</strong> every downstream check "
                            "(plugin probe, library settings, trickplay geometry) depends on "
                            "talking to Jellyfin. If this fails, the rest of the card is "
                            "meaningless.</p>"
                            "<p><strong>Common causes when it fails:</strong> wrong URL (e.g. "
                            "<code>localhost</code> from inside this container — see the URL field "
                            "tooltip in General), expired API key, Jellyfin restarting, or "
                            "network issue. Read-only check — fix the URL/credentials in the "
                            "General tab.</p>"
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
                "ok": version_ok,
                "severity": "critical",
                "checks": [
                    {
                        "id": "server_version",
                        "label": f"Jellyfin {version_value}" if version_value else "Jellyfin version",
                        "docs_anchor": "version",
                        "tooltip": "Jellyfin 10.10+ required",
                        "explanation": (
                            "<p><strong>What it checks:</strong> Jellyfin's reported version is at least "
                            "10.10, the release that introduced the <code>SaveTrickplayWithMedia</code> "
                            "code path this app depends on.</p>"
                            "<p><strong>Why it matters:</strong> on Jellyfin 10.9 and earlier, trickplay "
                            "is stored exclusively under <code>&lt;config&gt;/data/trickplay/</code> — "
                            "where this app never writes. Published previews would sit on disk invisible "
                            "forever.</p>"
                            "<p><strong>How to fix:</strong> upgrade Jellyfin via your package manager "
                            "or container image. Read-only check — there's no toggle here.</p>"
                        ),
                        "ok": version_ok,
                        "severity": "critical",
                        "current": version_value or "unknown",
                        "recommended": "10.10+",
                        "actions": {},
                        "reason": version_reason,
                        "meta": {},
                    }
                ],
            }
        )

        # --- Plugin section ------------------------------------------
        # User-facing labels avoid "Mode A/B" jargon — users don't know
        # what the modes mean and the row label is the first thing they
        # see. "Instant activation" vs. "Activates on next scan" is
        # self-explanatory. The ⓘ explanation still mentions Mode A/B
        # in parens for users following the docs.
        if plugin_installed:
            plugin_mode = "Instant activation"
        elif mode_a_library_names:
            # Plugin absent but a library has scan-extraction disabled
            # — the "next scan" fallback doesn't apply here, so saying
            # "Activates on next scan" would contradict the red critical
            # row below.
            plugin_mode = "Plugin required — not installed"
        else:
            plugin_mode = "Activates on next scan"
        if plugin_installed:
            plugin_actions = {
                "disable": {
                    "action": "uninstall_plugin",
                    "args": {},
                    "confirm": self._CONFIRM_UNINSTALL_PLUGIN,
                }
            }
        else:
            plugin_actions = {
                "enable": {
                    "action": "install_plugin",
                    "args": {},
                    "confirm": {
                        "kind": "button",
                        "phrase": "",
                        "body": (
                            "Adds the Media Preview Bridge plugin to Jellyfin and restarts the "
                            "server. Previews you generate after install will appear INSTANTLY "
                            "in Jellyfin clients instead of waiting for the next library scan "
                            "or the 3 AM scheduled task. Takes ~30 seconds for Jellyfin to "
                            "restart and re-index. Reversible via the Uninstall button."
                        ),
                    },
                }
            }
        # Plugin-absence is only a valid "Mode B choice" when every library
        # has scan-extraction enabled (so Jellyfin itself adopts our tiles
        # on the next scan). If any library is configured for Mode A —
        # ``ExtractTrickplayImagesDuringLibraryScan=false`` — plugin
        # absence means nothing ever registers the tiles and previews
        # never render in the player. In that state the row is a real
        # failure, not advisory.
        plugin_ok = plugin_installed or not plugin_required
        if plugin_installed:
            plugin_severity = "info"
            plugin_reason = plugin.get("error") or ""
            plugin_current = plugin.get("version") or "installed"
        elif plugin_required:
            plugin_severity = "critical"
            libs = ", ".join(mode_a_library_names)
            probe_error = plugin.get("error") or ""
            plugin_reason = (
                f"Plugin required because scan-time extraction is disabled on: {libs}. "
                f"Without the plugin, this app's published tiles never get adopted by "
                f"Jellyfin and scrubbing previews will not render. Either install the "
                f"plugin (for instant activation) or re-enable scan-time extraction on "
                f"the listed libraries (Jellyfin will adopt our tiles on its next scan)."
                + (f" Probe: {probe_error}" if probe_error else "")
            )
            plugin_current = "not installed"
        else:
            plugin_severity = "info"
            plugin_reason = plugin.get("error") or ""
            plugin_current = "not installed"
        sections.append(
            {
                "id": "plugin",
                "title": "Media Preview Bridge plugin",
                "docs_anchor": "plugin",
                "ok": plugin_ok,
                "severity": plugin_severity,
                "checks": [
                    {
                        "id": "plugin_installed",
                        "label": plugin_mode,
                        "docs_anchor": "plugin",
                        "tooltip": "Optional plugin for instant preview activation",
                        "explanation": (
                            "<p><strong>What it is:</strong> the Media Preview Bridge plugin is a "
                            "small Jellyfin plugin we publish alongside this app. When installed, "
                            "it exposes an internal endpoint this app calls to register published "
                            "previews directly with Jellyfin's trickplay manager — instantly, without "
                            "waiting for a library scan.</p>"
                            "<p><strong>How previews get activated:</strong></p>"
                            "<ul>"
                            "<li><strong>With the plugin installed:</strong> new previews appear in "
                            "the player the moment generation completes. Near-zero latency.</li>"
                            "<li><strong>Without the plugin:</strong> Jellyfin adopts our tiles on "
                            "its next library scan (usually within minutes) or at worst on the 3 AM "
                            "scheduled 'Refresh Trickplay Images' task. Fully functional — just slower.</li>"
                            "</ul>"
                            "<p><strong>When the plugin is required:</strong> if any library has "
                            "<code>ExtractTrickplayImagesDuringLibraryScan</code> set to <em>false</em> "
                            "(scan-time extraction disabled), plugin absence is a hard "
                            "failure — nothing registers our tiles and the scrubber stays blank. "
                            "Either install the plugin or enable scan-extraction on those libraries.</p>"
                            "<p><strong>Install / uninstall:</strong> both require a Jellyfin restart "
                            "(~30 seconds). Published tiles stay on disk either way — switching modes "
                            "is non-destructive and reversible.</p>"
                        ),
                        "ok": plugin_ok,
                        "severity": plugin_severity,
                        "current": plugin_current,
                        "recommended": "installed" if plugin_required else "installed (optional)",
                        "actions": plugin_actions,
                        "reason": plugin_reason,
                        "meta": {
                            "version": plugin.get("version") or "",
                            "mode_a_libraries": list(mode_a_library_names),
                            "plugin_required": plugin_required,
                        },
                    }
                ],
            }
        )

        # --- Library settings — per-library per-flag rows ------------
        # ``folders`` was fetched above (needed for the plugin section's
        # Mode A detection). Reuse it here instead of hitting Jellyfin twice.
        recommended = self._recommended_settings()
        library_checks: list[dict[str, Any]] = []

        library_section_ok = True
        library_severity = "info"
        if isinstance(folders, list):
            for raw in folders:
                if not isinstance(raw, dict):
                    continue
                lib_id = str(raw.get("ItemId") or raw.get("Id") or raw.get("Name") or "")
                lib_name = str(raw.get("Name") or "")
                options = raw.get("LibraryOptions") or {}
                for flag, label, want, severity, rationale in recommended:
                    current = bool(options.get(flag, False))
                    row_ok = current == want
                    if not row_ok:
                        library_section_ok = False
                        if severity == "critical":
                            library_severity = "critical"
                        elif library_severity != "critical":
                            library_severity = "recommended"
                    meta = self._FLAG_METADATA.get(flag, {})
                    actions = self._flag_actions(flag, current, plugin_installed)
                    # Scope actions to THIS library rather than the wildcard
                    # so a broken flag on one library doesn't surprise users
                    # with a server-wide flip.
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
                            # Rich-HTML multi-paragraph what/why/impact for
                            # the ⓘ popover. Falls back to tooltip + rationale
                            # when a flag hasn't been enriched yet so old
                            # rows still render something.
                            "explanation": meta.get("explanation")
                            or f"<p>{meta.get('tooltip', '') or ''}</p><p>{rationale}</p>",
                            "ok": row_ok,
                            "severity": severity,
                            "current": current,
                            "recommended": want,
                            "actions": actions,
                            "reason": None if row_ok else rationale,
                            "meta": {"flag": flag, "library_id": lib_id, "library_name": lib_name},
                        }
                    )

        sections.append(
            {
                "id": "library_settings",
                "title": "Library settings",
                "docs_anchor": "library-settings",
                "ok": library_section_ok,
                "severity": library_severity,
                "checks": library_checks,
            }
        )

        # --- Server-wide TrickplayOptions geometry -------------------
        options_check = self._check_trickplay_options()
        server_ok = bool(options_check.get("ok"))
        server_actions: dict[str, Any] = {}
        if not server_ok:
            server_actions["enable"] = {
                "action": "sync_trickplay_options",
                "args": {},
                "confirm": {
                    "kind": "button",
                    "phrase": "",
                    "body": (
                        "Writes this app's tile geometry (TileWidth, TileHeight, Interval, "
                        "WidthResolutions) back to Jellyfin's server-wide TrickplayOptions so "
                        "the scrubber renders the correct pixel range for each tile. "
                        "Existing admin-customised fields (like additional resolutions) are "
                        "preserved. Non-destructive — no tiles are deleted and no restart is "
                        "required. The change takes effect immediately for newly served previews."
                    ),
                },
            }
        sections.append(
            {
                "id": "server_options",
                "title": "Server trickplay options",
                "docs_anchor": "trickplay-options",
                "ok": server_ok,
                "severity": "critical" if not server_ok else "info",
                "checks": [
                    {
                        "id": "trickplay_geometry",
                        "label": "Tile geometry matches adapter",
                        "docs_anchor": "trickplay-options",
                        "tooltip": "Server tile geometry must match what this app writes",
                        "explanation": (
                            "<p><strong>What it checks:</strong> Jellyfin's server-wide "
                            "<code>TrickplayOptions</code> — tile width, tile height, frame interval, "
                            "and resolution list — match the geometry this app uses when writing "
                            "tile sheets.</p>"
                            "<p><strong>Why it matters:</strong> Jellyfin synthesises the client-"
                            "facing <code>TrickplayInfo</code> row from server-wide "
                            "<code>TrickplayOptions</code> VERBATIM — not measured from the tile "
                            "files themselves. A mismatch (e.g. server <code>TileWidth=8</code> vs "
                            "app <code>10</code>) means Jellyfin tells the client to slice tiles "
                            "at the wrong pixel coordinates. The preview loads, but renders "
                            "wrong: stretched, sheared, or showing the wrong frame at each "
                            "scrubber position.</p>"
                            "<p><strong>What 'Sync options' does:</strong> rewrites only the "
                            "fields we control (TileWidth, TileHeight, Interval, adds our width "
                            "to WidthResolutions if missing) and POSTs the full config back. "
                            "Admin-customised fields are preserved. No restart required; no "
                            "tiles deleted.</p>"
                        ),
                        "ok": server_ok,
                        "severity": "critical" if not server_ok else "info",
                        "current": options_check.get("server"),
                        "recommended": options_check.get("ours"),
                        "actions": server_actions,
                        "reason": options_check.get("reason") or None,
                        "meta": {},
                    }
                ],
            }
        )

        # --- Vendor-side extraction (advisory for Jellyfin) ----------
        vendor_probe_ok = True
        vendor_probe_reason = ""
        try:
            extraction_status = self.get_vendor_extraction_status()
        except Exception as exc:
            logger.debug("Vendor-extraction status probe failed for {!r}: {}", self.name, exc)
            extraction_status = {"extracting_count": 0, "stopped_count": 0, "skipped_count": 0, "total": 0}
            vendor_probe_ok = False
            vendor_probe_reason = f"Could not read library extraction state: {exc}"
        stopped = extraction_status.get("stopped_count", 0)
        extracting = extraction_status.get("extracting_count", 0)
        if not vendor_probe_ok:
            vendor_current = "unknown (probe failed)"
        elif stopped + extracting:
            vendor_current = f"stopped on {stopped}/{stopped + extracting}"
        else:
            vendor_current = "unknown"
        sections.append(
            {
                "id": "vendor_extraction",
                "title": "Vendor-side preview generation",
                "docs_anchor": "vendor-extraction",
                # Advisory only — Jellyfin re-enabling its own extraction is
                # wasteful (dup work) but never breaks playback. When the
                # probe itself fails we surface that (ok=False, info) so
                # the UI doesn't lie about state we couldn't read.
                "ok": vendor_probe_ok,
                "severity": "info",
                "checks": [
                    {
                        "id": "vendor_extraction_state",
                        "label": "Jellyfin scan-time extraction",
                        "docs_anchor": "vendor-extraction",
                        "tooltip": "Stop Jellyfin running its own trickplay generation",
                        "explanation": (
                            "<p><strong>What this controls:</strong> whether Jellyfin runs its own "
                            "trickplay extraction during library scans across every configured "
                            "library in one batch. This is a shortcut for flipping "
                            "<code>ExtractTrickplayImagesDuringLibraryScan</code> + "
                            "<code>SaveTrickplayWithMedia</code> on every library at once.</p>"
                            "<p><strong>Why we recommend stopping it:</strong> this app owns "
                            "preview generation end-to-end (with GPU acceleration, HDR tonemapping, "
                            "frame-reuse caching, etc.) so letting Jellyfin ALSO extract tiles "
                            "during scans is pure duplicate CPU. Published tiles get adopted via "
                            "the plugin (Mode A, instant) or the scan-adoption path (Mode B); "
                            "either way Jellyfin doesn't need to generate its own.</p>"
                            "<p><strong>What happens if you re-enable:</strong> Jellyfin starts "
                            "generating tiles during scans in parallel to this app's output. "
                            "Wasteful but non-destructive — both sets of tiles end up in the "
                            "same <code>.trickplay/</code> directory structure, and whichever "
                            "gets registered first wins.</p>"
                        ),
                        "ok": vendor_probe_ok,
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
                                        "Stops Jellyfin running its own trickplay extraction "
                                        "during library scans across all libraries. "
                                        "Recommended when this app owns preview generation. "
                                        "Non-destructive — existing tiles stay on disk and "
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
                                        "Re-enables Jellyfin's scan-time trickplay extraction "
                                        "across all libraries. Jellyfin will generate its OWN "
                                        "preview tiles in parallel to this app — duplicate CPU, "
                                        "but no data loss. Useful only if you want Jellyfin to "
                                        "take over preview generation and plan to stop using "
                                        "this app for the affected libraries."
                                    ),
                                },
                            },
                        },
                        "reason": vendor_probe_reason or None,
                        "meta": extraction_status,
                    }
                ],
            }
        )

        # --- Scheduled "Generate Trickplay Images" task --------------
        # Jellyfin ships with a daily 3 AM task that walks every video
        # and generates its own trickplay tiles via FFmpeg. The decision
        # to keep this task ON or OFF depends entirely on whether the
        # Media Preview Bridge plugin is installed:
        #
        #   Plugin installed (Mode A): this app's published tiles are
        #     registered instantly via a direct DB-write API call. The
        #     scheduled task ends up re-doing work that's already done,
        #     burning CPU + competing with library scans. Recommend OFF.
        #
        #   Plugin NOT installed (Mode B): the scheduled task IS the
        #     only path by which Jellyfin ever sees this app's tiles.
        #     Disabling it silently breaks trickplay. Recommend ON.
        #
        # The check below renders one row in either mode so users see
        # the relationship explicitly. Severity is conditional: amber
        # "Recommended" when we're suggesting disable for a Mode A user;
        # red "Critical" when we're warning a Mode B user that disabling
        # would break trickplay registration entirely.
        sched_state = self.get_scheduled_trickplay_state()
        if sched_state.get("found"):
            triggers_count = int(sched_state.get("triggers_count") or 0)
            task_state = (sched_state.get("state") or "").lower()
            task_running_note = (
                " (currently running — Jellyfin is mid-pass right now)" if task_state == "running" else ""
            )

            sched_explanation_common = (
                "<p><strong>What this task does:</strong> Jellyfin's built-in "
                "<code>Generate Trickplay Images</code> scheduled task runs daily at 3 AM "
                "by default. It walks every video in your libraries and creates trickplay "
                "tiles (the scrub-bar previews) for any file that doesn't already have "
                "them, using its own FFmpeg pass.</p>"
                "<p><strong>How this app handles trickplay:</strong> when a webhook fires "
                "(Sonarr/Radarr import), this app generates tiles to disk and tells "
                "Jellyfin about them. The path depends on whether the Media Preview Bridge "
                "plugin is installed:</p>"
                "<ul>"
                "<li><strong>With the plugin (Mode A):</strong> this app calls the plugin's "
                "<code>/MediaPreviewBridge/Trickplay/{itemId}</code> endpoint, which writes "
                "the trickplay row in Jellyfin's database directly. Registration is instant. "
                "The scheduled daily task ends up scanning the same files Jellyfin already "
                "knows about — wasted CPU and IO that also competes with library scans "
                "(slower scans = longer retry windows when new files arrive).</li>"
                "<li><strong>Without the plugin (Mode B):</strong> this app writes tiles to "
                "disk and crosses its fingers. Jellyfin's only chance to discover them is "
                "the daily scheduled task. Disabling it means tiles sit on disk forever "
                "with no DB row — trickplay silently never appears in the player.</li>"
                "</ul>"
            )

            if plugin_installed:
                # Mode A — plugin handles registration. Daily task is wasted CPU.
                if triggers_count > 0:
                    sched_check = {
                        "id": "scheduled_trickplay_task",
                        "label": "Jellyfin's daily 'Generate Trickplay Images' task",
                        "docs_anchor": "scheduled-trickplay",
                        "tooltip": (
                            "You have the Bridge plugin installed — this app already registers "
                            "trickplay instantly. The daily task just re-does the work and slows "
                            "library scans. Recommend disabling it."
                        ),
                        "explanation": (
                            sched_explanation_common + "<p><strong>Your setup:</strong> the Bridge plugin <em>is</em> "
                            "installed, so disabling the daily task is safe — trickplay "
                            "registration will continue to work instantly via the plugin's "
                            "direct DB write. You'll free up CPU and your library scans will "
                            "finish faster (which also shortens the retry window when new "
                            "files arrive before Jellyfin has indexed them).</p>"
                            "<p><strong>If you ever uninstall the plugin:</strong> re-enable "
                            "this task — it becomes the only way Jellyfin discovers tiles this "
                            "app published.</p>"
                        ),
                        "ok": False,
                        "severity": "recommended",
                        "current": f"enabled ({triggers_count} active trigger{'s' if triggers_count != 1 else ''}){task_running_note}",
                        "recommended": "disabled (Bridge plugin handles registration)",
                        "actions": {
                            "disable": {
                                "action": "set_scheduled_trickplay",
                                "args": {"enabled": False},
                                "confirm": {
                                    "kind": "button",
                                    "phrase": "",
                                    "body": (
                                        "Clears all triggers on Jellyfin's daily "
                                        "<code>Generate Trickplay Images</code> task. The task "
                                        "will no longer auto-fire at 3 AM. You can still run it "
                                        "manually from Jellyfin's Dashboard → Scheduled Tasks. "
                                        "<br><br><strong>Why this is safe for you:</strong> the "
                                        "Bridge plugin is installed, so this app registers "
                                        "trickplay instantly — Jellyfin already knows about every "
                                        "tile this app publishes."
                                    ),
                                },
                            },
                            "enable": {
                                "action": "set_scheduled_trickplay",
                                "args": {"enabled": True},
                                "confirm": {
                                    "kind": "button",
                                    "phrase": "",
                                    "body": (
                                        "Restores the default daily 3 AM trigger on the "
                                        "<code>Generate Trickplay Images</code> task. Useful if "
                                        "you plan to uninstall the Bridge plugin or want a "
                                        "safety-net pass that re-scans for missing tiles."
                                    ),
                                },
                            },
                        },
                        "reason": None,
                        "meta": sched_state,
                    }
                else:
                    sched_check = {
                        "id": "scheduled_trickplay_task",
                        "label": "Jellyfin's daily 'Generate Trickplay Images' task",
                        "docs_anchor": "scheduled-trickplay",
                        "tooltip": "Disabled — the Bridge plugin handles registration directly.",
                        "explanation": (
                            sched_explanation_common + "<p><strong>Your setup:</strong> the Bridge plugin handles "
                            "registration and the daily task is disabled. Optimal — no duplicate "
                            "work, library scans aren't fighting an extra background pass.</p>"
                        ),
                        "ok": True,
                        "severity": "info",
                        "current": "disabled (no triggers)",
                        "recommended": "disabled (Bridge plugin handles registration)",
                        "actions": {
                            "enable": {
                                "action": "set_scheduled_trickplay",
                                "args": {"enabled": True},
                                "confirm": {
                                    "kind": "button",
                                    "phrase": "",
                                    "body": (
                                        "Restores the default daily 3 AM trigger. Useful if you "
                                        "plan to uninstall the Bridge plugin and need Jellyfin's "
                                        "scheduled task to take over registration."
                                    ),
                                },
                            },
                        },
                        "reason": None,
                        "meta": sched_state,
                    }
            else:
                # Mode B — no plugin. The daily task is the registration path.
                if triggers_count > 0:
                    sched_check = {
                        "id": "scheduled_trickplay_task",
                        "label": "Jellyfin's daily 'Generate Trickplay Images' task",
                        "docs_anchor": "scheduled-trickplay",
                        "tooltip": (
                            "Keep enabled — without the Bridge plugin, this task is how Jellyfin "
                            "discovers the tiles this app publishes."
                        ),
                        "explanation": (
                            sched_explanation_common + "<p><strong>Your setup:</strong> the Bridge plugin is NOT "
                            "installed, so this task is the only way Jellyfin ever sees the "
                            "tiles this app publishes. Keep it enabled.</p>"
                            "<p><strong>Better alternative:</strong> install the Media Preview "
                            "Bridge plugin (separate row in this card) for instant registration "
                            "instead of waiting up to 24 hours for the daily pass to run.</p>"
                        ),
                        "ok": True,
                        "severity": "info",
                        "current": f"enabled ({triggers_count} active trigger{'s' if triggers_count != 1 else ''}){task_running_note}",
                        "recommended": "keep enabled (no Bridge plugin)",
                        "actions": {},
                        "reason": None,
                        "meta": sched_state,
                    }
                else:
                    sched_check = {
                        "id": "scheduled_trickplay_task",
                        "label": "Jellyfin's daily 'Generate Trickplay Images' task",
                        "docs_anchor": "scheduled-trickplay",
                        "tooltip": (
                            "Critical: without the Bridge plugin AND without this task running, "
                            "Jellyfin will never see the tiles this app publishes."
                        ),
                        "explanation": (
                            sched_explanation_common + "<p><strong>Your setup:</strong> the Bridge plugin is NOT "
                            "installed AND the daily task has no triggers. Tiles this app "
                            "publishes will sit on disk indefinitely with no Jellyfin DB row "
                            "— trickplay never appears in the player.</p>"
                            "<p><strong>Fix:</strong> either install the Bridge plugin "
                            "(recommended — instant registration) or re-enable this task "
                            "(slow — registration happens up to 24h after each new file).</p>"
                        ),
                        "ok": False,
                        "severity": "critical",
                        "current": "disabled (no triggers)",
                        "recommended": "enabled (no Bridge plugin → this is the only registration path)",
                        "actions": {
                            "enable": {
                                "action": "set_scheduled_trickplay",
                                "args": {"enabled": True},
                                "confirm": {
                                    "kind": "button",
                                    "phrase": "",
                                    "body": (
                                        "Restores the default daily 3 AM trigger. Without this "
                                        "task AND without the Bridge plugin, Jellyfin has no way "
                                        "to discover the tiles this app publishes — trickplay "
                                        "never appears in the player."
                                    ),
                                },
                            },
                        },
                        "reason": None,
                        "meta": sched_state,
                    }

            sched_section_ok = bool(sched_check.get("ok"))
            sched_section_severity = sched_check["severity"]
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
            sched_section_ok = True  # task not present → nothing to flag

        overall_ok = (
            connection_ok and version_ok and plugin_ok and library_section_ok and server_ok and sched_section_ok
        )
        return {
            "vendor": "jellyfin",
            "overall_ok": overall_ok,
            "sections": sections,
        }

    def trickplay_readiness(self) -> dict[str, Any]:
        """Legacy alias for :meth:`previews_readiness` returning the legacy shape.

        The legacy endpoint (``/trickplay-readiness``) and any external
        caller that reads fields like ``plugin.mode`` or
        ``trickplay_options`` keeps working: this method builds that
        legacy dict from the same probes the unified path uses.
        """
        plugin = self.check_plugin_installed()

        version_value = ""
        version_ok = True
        version_fix_kind: str | None = None
        version_reason = ""
        try:
            response = self._request("GET", "/System/Info")
            response.raise_for_status()
            data = response.json() or {}
            version_value = str(data.get("Version") or "") or ""
            parsed = self._parse_version_tuple(version_value)
            if parsed and parsed < (10, 10):
                version_ok = False
                version_fix_kind = "upgrade_jellyfin"
                version_reason = (
                    f"Jellyfin {version_value} is pre-10.10 — SaveTrickplayWithMedia "
                    "isn't supported. Upgrade to 10.10+ for native adoption."
                )
        except Exception as exc:
            logger.debug("Version probe failed for {!r}: {}", self.name, exc)
            version_reason = f"Could not read /System/Info: {exc}"

        library_issues = self.check_settings_health()
        options_check = self._check_trickplay_options()

        if plugin.get("installed"):
            mode = "plugin_instant"
        elif any(i.flag == "ExtractTrickplayImagesDuringLibraryScan" and not i.current for i in library_issues):
            mode = "scan_nudge_pending"
        else:
            mode = "scan_nudge"

        overall_ok = version_ok and not library_issues and options_check["ok"]

        return {
            "version": {
                "ok": version_ok,
                "value": version_value,
                "fix_kind": version_fix_kind,
                "reason": version_reason,
            },
            "plugin": {
                "installed": bool(plugin.get("installed")),
                "version": plugin.get("version") or "",
                "error": plugin.get("error") or "",
                "mode": mode,
            },
            "library_settings": {
                "ok": not library_issues,
                "issues": [
                    {
                        "library_id": i.library_id,
                        "library_name": i.library_name,
                        "flag": i.flag,
                        "label": i.label,
                        "current": i.current,
                        "recommended": i.recommended,
                        "severity": i.severity,
                        "rationale": i.rationale,
                    }
                    for i in library_issues
                ],
            },
            "trickplay_options": options_check,
            "overall_ok": overall_ok,
        }

    def trickplay_fix_all(self, *, install_plugin: bool = True) -> dict[str, Any]:
        """Auto-fix every readiness issue in one call.

        Steps (each produces a row in the returned ``steps`` list):

        1. ``install_plugin()`` — only if ``install_plugin=True`` AND the
           plugin isn't already installed. This is the only step that
           restarts Jellyfin, so opt-in by the user.
        2. ``apply_recommended_settings()`` — plugin-aware flag fixes.
        3. ``sync_trickplay_options()`` — server-wide geometry sync.

        Mirrors the existing ``install_plugin`` step-list shape so the
        UI's progress component can render both flows identically.
        """
        result: dict[str, Any] = {"steps": [], "ok": True, "error": ""}

        def _record(step: str, ok: bool, detail: str = "") -> None:
            result["steps"].append({"step": step, "ok": ok, "detail": detail})
            if not ok:
                result["ok"] = False
                if not result["error"]:
                    result["error"] = detail

        # ALWAYS probe the plugin first so the per-instance cache is
        # warm before apply_recommended_settings reads it. Without
        # this, a fresh JellyfinServer instance (every HTTP request
        # spawns one) sees cache=None and defaults to Mode B
        # recommendations — which is WRONG for a plugin-installed
        # setup. Probing here costs ~200ms and unblocks the correct
        # Mode A settings flips below.
        plugin_state = self.check_plugin_installed()

        if install_plugin:
            if plugin_state.get("installed"):
                _record("install_plugin", True, "already installed")
            else:
                try:
                    install_outcome = self.install_plugin()
                except Exception as exc:
                    _record("install_plugin", False, str(exc))
                    return result
                if install_outcome.get("ok"):
                    _record(
                        "install_plugin",
                        True,
                        "queued — poll plugin status after restart (~30s)",
                    )
                else:
                    _record(
                        "install_plugin",
                        False,
                        install_outcome.get("error") or "install failed",
                    )
                    return result

        settings_outcome = self.apply_recommended_settings()
        if "_global" in settings_outcome:
            _record("apply_recommended_settings", False, settings_outcome["_global"])
        else:
            errors = [k for k, v in settings_outcome.items() if not str(v).startswith("ok")]
            if errors:
                _record(
                    "apply_recommended_settings",
                    False,
                    f"{len(errors)} issue(s): {errors[0]}",
                )
            else:
                _record(
                    "apply_recommended_settings",
                    True,
                    f"applied {len(settings_outcome)} library change(s)",
                )

        geometry_outcome = self.sync_trickplay_options()
        if geometry_outcome["ok"]:
            _record("sync_trickplay_options", True, "server geometry matches adapter")
        else:
            _record(
                "sync_trickplay_options",
                False,
                geometry_outcome.get("error") or "TrickplayOptions sync failed",
            )

        return result

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
