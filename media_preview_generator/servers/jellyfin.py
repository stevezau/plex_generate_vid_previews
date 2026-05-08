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

    def trickplay_readiness(self) -> dict[str, Any]:
        """One-call readiness probe for the Edit-Server modal.

        Aggregates four independent checks:

        1. **Version** — Jellyfin 10.10+ is required for the
           ``SaveTrickplayWithMedia`` code path. Older versions look in
           ``<config>/data/trickplay/`` where we never write.
        2. **Plugin** — Media Preview Bridge presence. Informational for
           users who opt out; drives Mode A vs Mode B settings
           recommendations.
        3. **Per-library settings** — uses the plugin-aware
           ``_recommended_settings``. With plugin → recommend scan-extraction
           OFF. Without plugin → recommend scan-extraction ON (needed for
           adoption trigger).
        4. **Server-wide TrickplayOptions** — tile geometry MUST match our
           adapter or clients render broken previews silently.

        Returns a dict the UI card renders as a stoplight panel. Each
        section carries ``ok`` + an optional ``fix_kind`` string the UI
        maps to a button action.
        """
        # Probe plugin first — all downstream checks depend on its state.
        plugin = self.check_plugin_installed()

        # Version — requires a /System/Info call; tolerate failure.
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

        # Mode hint string — drives the "Activation mode" row on the UI card.
        if plugin.get("installed"):
            mode = "plugin_instant"
        elif any(i.flag == "ExtractTrickplayImagesDuringLibraryScan" and not i.current for i in library_issues):
            # User hasn't fixed scan-ext yet — Mode B won't activate until they do.
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
