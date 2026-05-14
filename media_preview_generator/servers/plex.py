"""Plex implementation of the :class:`MediaServer` interface.

This is a thin façade over the existing :mod:`media_preview_generator.plex_client`
helpers so the rest of the codebase can be migrated to the abstract interface
without rewriting Plex-specific logic. As the multi-server refactor lands, the
inline calls in :mod:`processing.generator` and :mod:`web.webhooks` are
re-routed through this class.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import requests
import urllib3
from loguru import logger

from .base import (
    ConnectionResult,
    FlagTarget,
    HealthCheckIssue,
    Library,
    MediaItem,
    MediaServer,
    ServerConfig,
    ServerType,
    WebhookEvent,
)

if TYPE_CHECKING:
    from ..config import Config


def _plex_item_id(m: Any) -> str:
    """Return Plex's bare ``ratingKey`` for ``m`` (e.g. ``"54321"``).

    Why: PlexAPI's ``m.key`` is the URL ``/library/metadata/<id>`` — passing it
    downstream as ``item_id`` doubles the prefix when ``PlexBundleAdapter`` builds
    ``/library/metadata/{item_id}/tree``, which silently 404s and reports
    ``skipped_not_indexed`` for every item in a Plex-pinned scan.
    """
    raw = getattr(m, "ratingKey", None)
    if raw not in (None, ""):
        return str(raw)
    key = str(getattr(m, "key", "") or "")
    return key.rsplit("/", 1)[-1] if key else key


def _extract_plex_bundle_metadata(m: Any) -> tuple[tuple[str, str], ...]:
    """Capture every MediaPart's ``(hash, file)`` pair from a plexapi item.

    plexapi's ``section.search()`` returns Movie/Episode objects with
    ``item.media[*].parts[*]`` already loaded — including the ``hash``
    attribute that PlexBundleAdapter would otherwise re-fetch one item
    at a time via ``/library/metadata/{id}/tree``. Capturing the hash
    here turns N sequential network round-trips per scan into 0.

    Returns an empty tuple when the item carries no parts (rare, but
    can happen for items still being analysed). The publisher then
    falls back to the per-item ``/tree`` lookup so behaviour matches
    the pre-streamlining path for un-indexed items.
    """
    pairs: list[tuple[str, str]] = []
    for media in getattr(m, "media", None) or []:
        for part in getattr(media, "parts", None) or []:
            h = str(getattr(part, "hash", "") or "")
            f = str(getattr(part, "file", "") or "")
            if h and f:
                pairs.append((h, f))
    return tuple(pairs)


def _synthesize_legacy_config(cfg: ServerConfig) -> SimpleNamespace:
    """Build a legacy Config-shaped namespace from a per-server ServerConfig.

    The ``plex_client`` helpers all key off ``config.plex_*`` attributes
    (``plex_url`` / ``plex_token`` / ``plex_verify_ssl`` / ``plex_timeout``
    / ``path_mappings`` / ``exclude_paths`` / ``plex_libraries`` /
    ``plex_library_ids`` / ``plex_config_folder`` /
    ``plex_bif_frame_interval`` / ``server_display_name``). Rather than
    refactor every caller, the wrapper synthesizes a SimpleNamespace with
    those fields from a ServerConfig — single point of translation.
    """
    enabled_lib_ids = [str(lib.id) for lib in (cfg.libraries or []) if getattr(lib, "enabled", True)]
    return SimpleNamespace(
        plex_url=cfg.url or "",
        plex_token=str((cfg.auth or {}).get("token") or ""),
        plex_verify_ssl=bool(cfg.verify_ssl),
        plex_timeout=int(cfg.timeout) if cfg.timeout else 10,
        server_display_name=cfg.name,
        path_mappings=list(cfg.path_mappings or []),
        exclude_paths=list(cfg.exclude_paths or []),
        plex_library_ids=enabled_lib_ids,
        plex_libraries=[],  # legacy name-based selector — superseded by ids
        plex_config_folder=(cfg.output or {}).get("plex_config_folder", "/plex"),
        plex_bif_frame_interval=int((cfg.output or {}).get("frame_interval") or 10),
    )


class PlexServer(MediaServer):
    """Wrap a single Plex Media Server in the :class:`MediaServer` interface.

    Accepts either:

    * A :class:`ServerConfig` (new canonical shape, used by the multi-server
      registry) — internally synthesized into a legacy Config-shaped
      namespace so existing ``plex_client`` helpers keep working unchanged.
    * A duck-typed legacy ``Config`` (or test mock with ``plex_*`` attrs) —
      used as-is. Kept for the connection-probe shim in
      ``api_servers._instantiate_for_probe`` and for tests that build a
      single-Plex setup from the legacy global config.

    The underlying ``plexapi`` connection is created lazily on first use; the
    class is therefore cheap to instantiate from configuration without paying
    a round-trip cost.
    """

    def __init__(
        self,
        config: ServerConfig | Config | Any,
        *,
        server_id: str | None = None,
        name: str | None = None,
    ) -> None:
        if isinstance(config, ServerConfig):
            self._server_config: ServerConfig | None = config
            self._config = _synthesize_legacy_config(config)
            super().__init__(
                server_id=server_id or config.id,
                name=name or config.name,
            )
        else:
            # Duck-typed legacy Config (or test mock).
            self._server_config = None
            self._config = config
            super().__init__(
                server_id=server_id or "plex",
                name=name or "Plex",
            )
        self._plex = None  # type: ignore[assignment]
        # Double-checked-locking guard for ``_connect``. Without this,
        # N parallel workers cold-hitting the same PlexServer instance
        # all see ``self._plex is None`` simultaneously, all open a
        # fresh ``plexapi.PlexServer`` (each with its own TLS
        # handshake), and N-1 immediately leak to GC. Visible on
        # multi-file webhook jobs as N "Connecting to Plex…" log lines
        # firing within milliseconds (e.g. 24-file The Dry batch with
        # 4 workers → 4 connections in 4ms).
        self._plex_lock = threading.Lock()

    @property
    def type(self) -> ServerType:
        return ServerType.PLEX

    @property
    def config(self) -> Config:
        """Expose the wrapped :class:`Config` for transitional callers."""
        return self._config

    def _connect(self):
        """Lazily create the underlying ``plexapi`` server connection.

        Thread-safe via double-checked locking: the fast path (cache
        hit) skips the lock entirely, the slow path (cache miss)
        acquires the lock and re-checks before constructing — so under
        N parallel cold-hits exactly ONE connection is established.
        """
        plex = self._plex
        if plex is not None:
            return plex
        from ..plex_client import plex_server as _build_plex

        with self._plex_lock:
            if self._plex is None:
                self._plex = _build_plex(self._config)
        return self._plex

    def test_connection(self) -> ConnectionResult:
        """Probe the Plex server identity via ``GET /``.

        Mirrors the logic in ``web/routes/api_plex.py:test_plex_connection``
        but returns a structured :class:`ConnectionResult`. Never raises on
        transport errors; failures are reported via ``ok=False``.
        """
        url = (self._config.plex_url or "").rstrip("/")
        token = self._config.plex_token or ""
        verify_ssl = bool(getattr(self._config, "plex_verify_ssl", True))
        timeout = int(getattr(self._config, "plex_timeout", 10) or 10)

        if not url or not token:
            return ConnectionResult(ok=False, message="Plex URL and token are required")

        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        try:
            response = requests.get(
                f"{url}/",
                headers={"X-Plex-Token": token, "Accept": "application/json"},
                timeout=timeout,
                verify=verify_ssl,
            )
            response.raise_for_status()
            container = response.json().get("MediaContainer", {})
            return ConnectionResult(
                ok=True,
                server_id=container.get("machineIdentifier") or None,
                server_name=container.get("friendlyName") or None,
                version=container.get("version") or None,
                message="Connected",
            )
        except requests.exceptions.SSLError as e:
            return ConnectionResult(
                ok=False,
                message=(
                    f"SSL certificate verification failed: {e}. "
                    "If you're using a self-signed certificate, disable Verify SSL."
                ),
            )
        except requests.exceptions.Timeout:
            return ConnectionResult(
                ok=False,
                message=f"Connection to {url} timed out after {timeout}s",
            )
        except requests.exceptions.ConnectionError as e:
            return ConnectionResult(
                ok=False,
                message=f"Could not connect to Plex at {url}: {e}",
            )
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 401:
                msg = "Plex rejected the authentication token (401)"
            elif status == 403:
                msg = "Access denied by Plex server (403)"
            elif status == 404:
                msg = f"URL reachable but did not return Plex server identity (404). Check '{url}'"
            else:
                msg = f"Plex returned HTTP {status}"
            return ConnectionResult(ok=False, message=msg)
        except (ValueError, requests.RequestException) as e:
            return ConnectionResult(ok=False, message=f"Connection test failed: {e}")

    def list_libraries(self) -> list[Library]:
        """Enumerate libraries from Plex, applying the user's enabled-list filter.

        When constructed from a multi-server ``ServerConfig`` (the modern path),
        prefer the per-library ``enabled`` flag from that snapshot — this
        matches the Emby/Jellyfin behaviour and respects the user's UI tick
        even when ALL libraries are unticked. The legacy
        ``plex_library_ids`` / ``plex_libraries`` selectors stay as a fallback
        for the duck-typed legacy ``Config`` (and tests that pass a mock).

        Why: previously this method consulted ONLY the synthesized
        ``plex_library_ids``, which is empty when the user has unticked
        every library. Empty list was misread as "no filter" → ``enabled=True``
        for every section, so a Plex-pinned scan happily walked all
        libraries the user thought they had disabled.
        """
        from ..plex_client import retry_plex_call

        try:
            sections = retry_plex_call(self._connect().library.sections)
        except Exception as exc:
            logger.warning(
                "Could not list libraries on Plex server {!r} after retries: {}. "
                "Plex may be offline, restarting, or the token may have expired — "
                "test the connection in Settings → Media Servers.",
                self.name,
                exc,
            )
            return []

        # Modern path: an explicit per-library snapshot exists on ServerConfig.
        # Honour it directly so unticking ALL libraries means "none enabled".
        explicit_enabled: dict[str, bool] | None = None
        sc = self._server_config
        if sc is not None and sc.libraries:
            explicit_enabled = {str(lib.id): bool(getattr(lib, "enabled", True)) for lib in sc.libraries}

        # Legacy path (tests + duck-typed Config): fall back to ID/title filters.
        selected_ids = {str(s).strip() for s in (getattr(self._config, "plex_library_ids", None) or [])}
        selected_titles = {
            str(name).strip().lower()
            for name in (getattr(self._config, "plex_libraries", None) or [])
            if str(name).strip()
        }

        libraries: list[Library] = []
        for section in sections:
            section_key = str(getattr(section, "key", "") or "")
            section_title = str(getattr(section, "title", "") or "")
            locations = tuple(str(loc) for loc in (getattr(section, "locations", None) or []))
            if explicit_enabled is not None:
                # Library not in the snapshot at all → treat as disabled
                # (the user hasn't ticked it). Library in the snapshot →
                # respect its explicit enabled state.
                enabled = explicit_enabled.get(section_key, False)
            elif selected_ids:
                enabled = section_key in selected_ids
            elif selected_titles:
                enabled = section_title.lower() in selected_titles
            else:
                enabled = True
            libraries.append(
                Library(
                    id=section_key,
                    name=section_title,
                    remote_paths=locations,
                    enabled=enabled,
                    kind=getattr(section, "METADATA_TYPE", None),
                )
            )
        return libraries

    def set_vendor_extraction(
        self,
        *,
        scan_extraction: bool,
        library_ids: list[str] | None = None,
    ) -> dict[str, str]:
        """Toggle Plex's "Generate video preview thumbnails" per library.

        Used by the "Vendor-side preview generation" panel on the Edit
        Server modal. When THIS app handles preview generation, Plex's
        own scanner-thumbnail step is wasted CPU — Plex always loads
        our published BIF when it's present, so disabling Plex's own
        generation has no display impact.

        Plex's per-section preference is ``enableBIFGeneration`` —
        bool-as-string ``"0"`` or ``"1"`` accepted via plexapi's
        ``editAdvanced``. (NOT ``scannerThumbnailVideoFiles`` —
        that name doesn't exist on modern Plex sections; the
        BIF-generation toggle is the right field.)

        ``library_ids=None`` means "every library on this server".
        """
        from ..plex_client import retry_plex_call

        try:
            sections = retry_plex_call(self._connect().library.sections)
        except Exception as exc:
            return {"_global": f"failed to list sections: {exc}"}

        target = set(library_ids) if library_ids else None
        results: dict[str, str] = {}
        # Setting value goes to Plex as 0/1 in the query string.
        value = 1 if scan_extraction else 0
        for section in sections:
            # Issue #237: never write enableBIFGeneration on
            # music/photo libraries. The setting is video-specific,
            # and our readiness UI doesn't surface them. But the route
            # accepts library_ids=None (server-wide apply) and external
            # callers (curl / Tdarr / API consumers) could trigger
            # this path — defend at the write-site like Emby/Jellyfin.
            section_type = str(getattr(section, "type", "") or "")
            if section_type not in ("movie", "show"):
                continue
            section_key = str(getattr(section, "key", "") or "")
            if not section_key:
                continue
            if target is not None and section_key not in target:
                continue
            try:
                section.editAdvanced(enableBIFGeneration=value)
                results[section_key] = "ok"
                continue
            except Exception as exc:
                primary_msg = str(exc)
                # plexapi's editAdvanced sends
                # ``PUT /library/sections/{id}?agent=X&prefs[…]=…`` and
                # Plex 400s with *"unable to find built-in agent group
                # for provided 'agent'"* when X is a custom agent
                # (Sportarr / XBMCnfo / community agents). The
                # codebase used to treat that 400 as a permanent
                # "manual fix only" verdict.
                #
                # Verified 2026-05-11 against a live Sportarr library:
                # ``PUT /library/sections/{id}/prefs?enableBIFGeneration=N``
                # succeeds (HTTP 200, value actually changes). Same
                # subpath as the GET that ``section.settings()`` reads
                # from; this asymmetric write handler does NOT
                # re-validate the agent. So a 400 on the full-section
                # edit path is recoverable: retry via /prefs subpath
                # before surfacing failure.
                looks_like_agent_rejection = "agent" in primary_msg.lower() and "400" in primary_msg
                if not looks_like_agent_rejection:
                    logger.warning(
                        "Could not update Plex library {} BIF-generation preference on server {!r}: {}",
                        section_key,
                        self.name,
                        exc,
                    )
                    results[section_key] = f"error: {exc}"
                    continue

                fallback_error = self._set_bif_via_prefs_subpath(section, value)
                if fallback_error is None:
                    logger.info(
                        "Plex library {} on server {!r}: editAdvanced rejected the section's "
                        "custom agent; /prefs subpath PUT succeeded.",
                        section_key,
                        self.name,
                    )
                    results[section_key] = "ok"
                    continue

                logger.warning(
                    "Plex library {} on server {!r}: BOTH editAdvanced and /prefs subpath failed. "
                    "editAdvanced: {} | /prefs PUT: {}",
                    section_key,
                    self.name,
                    primary_msg,
                    fallback_error,
                )
                results[section_key] = f"error: {fallback_error}"
        return results

    def _set_bif_via_prefs_subpath(self, section: Any, value: int) -> str | None:
        """Write ``enableBIFGeneration`` via the section's ``/prefs`` subpath.

        Plex's ``PUT /library/sections/{id}/prefs?<setting>=<value>``
        endpoint is the asymmetric write counterpart to the GET that
        ``section.settings()`` uses to read prefs. Unlike the
        full-section edit handler at ``PUT /library/sections/{id}``
        (which plexapi's ``editAdvanced`` uses and which validates
        the section's agent against Plex's built-in registry), the
        ``/prefs`` subpath does NOT re-validate the agent — so it
        works for custom-agent libraries (Sportarr / XBMCnfo /
        community agents) that the full-section handler 400s on
        with *"unable to find built-in agent group for provided
        'agent'"*.

        Verified empirically on 2026-05-11 against a live Sportarr
        library: ``PUT /library/sections/12/prefs?enableBIFGeneration=0``
        returned 200 and the read-back value actually changed from
        ``true`` to ``false`` (and was restored to ``true`` cleanly).

        Returns ``None`` on success or the error string on failure so
        the caller can log Plex's verbatim response.
        """
        from urllib.parse import urlencode

        plex = self._connect()
        section_key = getattr(section, "key", None)
        if section_key is None:
            return "no section key on plexapi object"

        url = f"/library/sections/{section_key}/prefs?{urlencode({'enableBIFGeneration': value})}"
        try:
            plex.query(url, method=plex._session.put)
            return None
        except Exception as exc:
            return str(exc)

    def get_vendor_extraction_status(self) -> dict[str, Any]:
        """Audit per-section ``enableBIFGeneration`` to drive the Edit modal CTA.

        Reads the same field ``set_vendor_extraction`` writes — for each
        section we treat ``enableBIFGeneration == False`` as the
        recommended/stopped state (Plex isn't generating its own BIFs).
        Custom-agent sections often fail the audit the same way they fail
        ``set_vendor_extraction`` (HTTP 400 from plexapi's editAdvanced
        because Plex's section-edit endpoint validates the agent against
        a built-in registry); we count those as ``skipped`` so the UI can
        render the same "1 skipped (custom agent — toggle in Plex UI)"
        footnote that ``set_vendor_extraction`` does.

        Returns a dict with aggregate counts plus a per-library
        ``libraries`` list (``[{key, name, state}]``, where ``state`` is
        one of ``"extracting"`` / ``"stopped"`` / ``"skipped"``). The
        readiness probe uses the per-library detail to emit one
        actionable row per library; older callers using only the
        aggregate counts continue to work unchanged.
        """
        from ..plex_client import retry_plex_call

        try:
            sections = retry_plex_call(self._connect().library.sections)
        except Exception as exc:
            logger.debug("Vendor-extraction status probe failed for {!r}: {}", self.name, exc)
            return {
                "extracting_count": 0,
                "stopped_count": 0,
                "skipped_count": 0,
                "total": 0,
                "libraries": [],
            }

        extracting = stopped = skipped = 0
        libraries: list[dict[str, Any]] = []
        for section in sections:
            # Issue #237: skip music/photo libraries entirely. The
            # ``enableBIFGeneration`` setting only exists for video
            # libraries — for music/photo it's either absent (would land
            # in "skipped" with a misleading "Change in Plex UI" row that
            # tells the user to disable a toggle that doesn't exist) or
            # present but irrelevant. ``section.type`` is plexapi's raw
            # type string (movie/show/artist/photo) — same field
            # ``api_libraries.py`` uses to gate the library picker, so
            # this filter is consistent with what users see elsewhere
            # in the UI. ``METADATA_TYPE`` is intentionally not used: it
            # resolves to "episode" for TV (item-level type, not
            # section-level).
            section_type = str(getattr(section, "type", "") or "")
            if section_type not in ("movie", "show"):
                continue
            section_key = str(getattr(section, "key", "") or "")
            section_title = str(getattr(section, "title", "") or section_key or "Unknown library")
            try:
                # plexapi exposes per-section settings (the "Advanced"
                # tab in Plex web UI) via ``section.settings()`` —
                # NOT ``section.advanced``/``section.preferences`` which
                # don't exist. The relevant entry is enableBIFGeneration
                # (a Bool-typed Setting). The GET that reads settings
                # works for every library type (built-in OR custom
                # agent) — it doesn't validate the agent like the
                # full-section edit PUT does.
                settings = retry_plex_call(section.settings)
                bif_setting = next(
                    (s for s in settings if str(getattr(s, "id", "")) == "enableBIFGeneration"),
                    None,
                )
                if bif_setting is None:
                    skipped += 1
                    libraries.append({"key": section_key, "name": section_title, "state": "skipped"})
                    continue
                # Setting.value is True/False for bool settings.
                # No edit-probe needed at audit time: every library
                # (including custom-agent ones) is writable via the
                # ``/library/sections/{id}/prefs`` subpath that
                # ``set_vendor_extraction`` falls back to. Pre-fix the
                # audit did a no-op ``section.edit()`` to detect
                # custom-agent rejection and demoted those libraries
                # to "skipped" so the UI hid the Apply button. With
                # the /prefs fallback in place, that demotion was
                # wrong — Sports IS auto-fixable. Drop the probe.
                if bool(getattr(bif_setting, "value", False)):
                    extracting += 1
                    libraries.append({"key": section_key, "name": section_title, "state": "extracting"})
                else:
                    stopped += 1
                    libraries.append({"key": section_key, "name": section_title, "state": "stopped"})
            except Exception as exc:
                logger.debug(
                    "Could not audit Plex section {} BIF-generation on {!r}: {}",
                    section_key or "?",
                    self.name,
                    exc,
                )
                skipped += 1
                libraries.append({"key": section_key, "name": section_title, "state": "skipped"})

        return {
            "extracting_count": extracting,
            "stopped_count": stopped,
            "skipped_count": skipped,
            "total": extracting + stopped + skipped,
            "libraries": libraries,
        }

    # ------------------------------------------------------------------
    # Server-wide settings health check
    # ------------------------------------------------------------------
    #
    # Plex's relevant settings live in `Settings → Library` in the web UI
    # and are SERVER-WIDE (not per-section). They control whether new files
    # added by Sonarr/Radarr/etc. get auto-detected at all — without
    # FSEventLibraryUpdatesEnabled, our SKIPPED_NOT_IN_LIBRARY scan-nudge
    # is the *only* mechanism by which Plex notices a new file. Most users
    # don't know these flags exist or which way they should point;
    # surfacing them here removes the "why didn't it pick up the file?"
    # head-scratch.

    _PLEX_RECOMMENDED_PREFS: tuple[tuple[str, str, Any, str, str], ...] = (
        (
            "FSEventLibraryUpdatesEnabled",
            "Scan my library automatically",
            True,
            # Recommended (not critical): this app has its own scan-nudge
            # mechanism (SKIPPED_NOT_IN_LIBRARY) and webhook listeners, so
            # external scan drivers like autopulse/Tdarr are legitimate
            # alternatives. The opinion still surfaces — but as amber
            # advice, not a red "Must fix" banner.
            "recommended",
            "Without this, Plex doesn't react to filesystem events at all — your only signals "
            "for new files are this app's scan-nudges and the periodic timer. Most missed-file "
            "complaints come from this being off.",
        ),
        (
            "FSEventLibraryPartialScanEnabled",
            "Run a partial scan when changes are detected",
            True,
            "recommended",
            "When on, Plex only re-scans the directory that changed. Off = a full library scan "
            "every time a file is added — many minutes of work for a single new episode.",
        ),
        (
            "ScheduledLibraryUpdatesEnabled",
            "Scan my library periodically (safety net)",
            True,
            "recommended",
            "Belt-and-braces in case a filesystem event is missed (network mounts and "
            "container-restart edge cases). Keep on; the default 12 h interval is fine.",
        ),
    )

    def check_settings_health(self) -> list[HealthCheckIssue]:
        """Audit Plex's server-wide library-scan preferences.

        Reads ``GET /:/prefs`` once and emits one :class:`HealthCheckIssue`
        per mis-set preference. Server-wide flags get
        ``library_id=None`` / ``library_name=""`` — the UI groups
        these as "Server settings" rather than under any one library.
        """
        url = (self._config.plex_url or "").rstrip("/")
        token = self._config.plex_token or ""
        verify_ssl = bool(getattr(self._config, "plex_verify_ssl", True))
        timeout = int(getattr(self._config, "plex_timeout", 10) or 10)
        if not url or not token:
            return []

        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        try:
            response = requests.get(
                f"{url}/:/prefs",
                headers={"X-Plex-Token": token, "Accept": "application/json"},
                timeout=timeout,
                verify=verify_ssl,
            )
            response.raise_for_status()
            settings = response.json().get("MediaContainer", {}).get("Setting", [])
        except Exception as exc:
            logger.warning(
                "Could not load Plex preferences for health check on {!r}: {}. "
                "The health-check panel will report 'unavailable' until Plex is reachable.",
                self.name,
                exc,
            )
            return []

        # Index settings by id for cheap lookups.
        current_by_id = {str(s.get("id") or ""): s.get("value") for s in settings if isinstance(s, dict)}

        issues: list[HealthCheckIssue] = []
        for pref_id, label, recommended, severity, rationale in self._PLEX_RECOMMENDED_PREFS:
            current = current_by_id.get(pref_id)
            if current == recommended:
                continue
            issues.append(
                HealthCheckIssue(
                    library_id=None,
                    library_name="",
                    flag=pref_id,
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
        """Flip mis-set Plex preferences to their recommended values.

        Plex's preference update endpoint accepts a single PUT
        ``/:/prefs?<id>=<value>`` per change — there's no batch form.
        We issue one PUT per flag and aggregate per-flag outcomes
        keyed ``":<flag>"`` (empty library_id segment for server-wide
        prefs) so the UI's per-row display matches the
        :class:`HealthCheckIssue` row keys.
        """
        url = (self._config.plex_url or "").rstrip("/")
        token = self._config.plex_token or ""
        verify_ssl = bool(getattr(self._config, "plex_verify_ssl", True))
        timeout = int(getattr(self._config, "plex_timeout", 10) or 10)
        if not url or not token:
            return {"_global": "Plex URL and token required"}

        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        # Re-read current values so the apply path doesn't blindly POST
        # to flags that were already correct (Plex returns 200 either
        # way; this saves the user a confusing "applied 3" message when
        # in reality only 1 actually changed).
        try:
            response = requests.get(
                f"{url}/:/prefs",
                headers={"X-Plex-Token": token, "Accept": "application/json"},
                timeout=timeout,
                verify=verify_ssl,
            )
            response.raise_for_status()
            settings = response.json().get("MediaContainer", {}).get("Setting", [])
        except Exception as exc:
            return {"_global": f"failed to fetch preferences: {exc}"}

        current_by_id = {str(s.get("id") or ""): s.get("value") for s in settings if isinstance(s, dict)}

        target_flags = set(flags) if flags is not None else None
        results: dict[str, str] = {}

        for pref_id, _label, recommended, _sev, _rationale in self._PLEX_RECOMMENDED_PREFS:
            if target_flags is not None and pref_id not in target_flags:
                continue
            if current_by_id.get(pref_id) == recommended:
                continue
            # Plex's bool prefs accept "true"/"false" strings on the
            # query string. Ints go as their string form. The API
            # returns 200 with no body on success and 4xx on a
            # bad/unknown id.
            value_str = "true" if recommended is True else ("false" if recommended is False else str(recommended))
            try:
                put = requests.put(
                    f"{url}/:/prefs",
                    params={pref_id: value_str},
                    headers={"X-Plex-Token": token},
                    timeout=timeout,
                    verify=verify_ssl,
                )
                put.raise_for_status()
                results[f":{pref_id}"] = "ok"
            except Exception as exc:
                logger.warning(
                    "Could not update Plex preference {} on server {!r}: {}",
                    pref_id,
                    self.name,
                    exc,
                )
                results[f":{pref_id}"] = f"error: {exc}"

        return results

    # Per-flag UX metadata. All Plex pref toggles are non-destructive
    # (no data is deleted by flipping them) so confirm.kind is always
    # "button" and the body explains what enabling/disabling does.
    _PLEX_FLAG_METADATA: dict[str, dict[str, Any]] = {
        "FSEventLibraryUpdatesEnabled": {
            "check_id": "fsevent_updates",
            "docs_anchor": "fsevent-updates",
            "tooltip": "Plex's real-time filesystem watcher",
            "explanation": (
                "<p><strong>What it does:</strong> when on, Plex subscribes to filesystem "
                "events (inotify on Linux, FSEvents on macOS) and notices added / removed / "
                "renamed files in your library folders in real time.</p>"
                "<p><strong>Why we recommend on:</strong> most 'why didn't Plex pick up the "
                "file?' complaints trace back here. With this off, Plex only learns about new "
                "files when (a) this app's scan-nudges fire after a preview publish, or (b) "
                "the periodic timer (ScheduledLibraryUpdatesEnabled, below) runs. Both are "
                "best-effort; real-time eventing is the reliable path.</p>"
                "<p><strong>What happens if you disable it:</strong> Plex stops watching for "
                "file events. New Sonarr/Radarr imports become invisible to Plex until the "
                "periodic scan runs or something external nudges a scan. Non-destructive and "
                "reversible.</p>"
            ),
            "enable_body": (
                "Plex will watch library folders for filesystem changes and pick up new "
                "files in real time. This is the recommended state and eliminates most "
                "'Plex didn't notice the file' issues."
            ),
            "disable_body": (
                "Plex will stop watching the filesystem for new files. Adding files "
                "externally (Sonarr/Radarr, rsync, etc.) won't show up in Plex until the "
                "periodic scan runs or this app nudges Plex manually. Non-destructive and "
                "reversible."
            ),
        },
        "FSEventLibraryPartialScanEnabled": {
            "check_id": "fsevent_partial",
            "docs_anchor": "fsevent-partial",
            "tooltip": "Partial scan on filesystem change",
            "explanation": (
                "<p><strong>What it does:</strong> when a filesystem event fires (new file, "
                "move, etc.), Plex only re-scans the specific directory that changed instead "
                "of re-scanning the entire library.</p>"
                "<p><strong>Why we recommend on:</strong> a full library scan for a 100k-item "
                "Plex install can take many minutes — and with FSEvents enabled, you'd trigger "
                "a full scan every single time a file changes. Partial scan limits the work to "
                "the one directory that actually changed, typically finishing in seconds.</p>"
                "<p><strong>What happens if you disable it:</strong> every filesystem event "
                "triggers a full library scan. For small libraries this is fine; for large ones "
                "it's painful and can queue scans faster than they complete. Reversible.</p>"
            ),
            "enable_body": (
                "Plex will only re-scan the directory that changed when a filesystem event "
                "fires. Recommended — saves minutes per event on large libraries."
            ),
            "disable_body": (
                "Every filesystem event will trigger a FULL library scan. On large libraries "
                "this can be many minutes per added file, and scans may queue faster than "
                "they complete. Reversible, but rarely desirable."
            ),
        },
        "ScheduledLibraryUpdatesEnabled": {
            "check_id": "scheduled_scan",
            "docs_anchor": "scheduled-scan",
            "tooltip": "Periodic library scan safety net",
            "explanation": (
                "<p><strong>What it does:</strong> Plex runs a scheduled library scan at a "
                "fixed interval (default: every hour) regardless of filesystem events.</p>"
                "<p><strong>Why we recommend on:</strong> a belt-and-braces safety net for "
                "cases where FSEvents can miss changes — network mounts (SMB/NFS don't always "
                "propagate inotify), container restarts, or Plex bugs. Even with real-time "
                "events enabled, the scheduled scan catches anything that slipped through.</p>"
                "<p><strong>What happens if you disable it:</strong> Plex only picks up files "
                "via real-time events (if on) or manual scans. On a network-mount setup where "
                "FSEvents are unreliable, files can sit invisible for hours. Reversible.</p>"
            ),
            "enable_body": (
                "Plex will run a scheduled library scan at its configured interval as a "
                "safety net, catching anything missed by the real-time watcher. Recommended, "
                "especially for network-mount setups."
            ),
            "disable_body": (
                "Plex will stop running scheduled library scans. File pickup depends entirely "
                "on real-time filesystem events (if enabled) or manual scans. On network-mount "
                "setups this can leave files invisible for hours. Reversible."
            ),
        },
    }

    def _plex_flag_actions(self, flag: str, current: Any) -> dict[str, Any]:
        """Build the ``actions`` blob for a Plex pref row.

        Every toggle carries a button-confirm blob with an explanation
        of what the action does — users always see what they're about
        to do before the POST fires.
        """
        meta = self._PLEX_FLAG_METADATA.get(flag, {})
        actions: dict[str, Any] = {}
        # Plex prefs return bool values; normalise for comparison.
        current_bool = bool(current) if not isinstance(current, str) else current.lower() == "true"
        if not current_bool:
            actions["enable"] = {
                "action": "apply_flag",
                "args": {"flag": flag, "value": True},
                "confirm": {
                    "kind": "button",
                    "phrase": "",
                    "body": meta.get("enable_body") or "",
                },
            }
        if current_bool:
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
        """Set each ``(flag, value)`` pair explicitly on Plex's server-wide prefs.

        Plex's preference update endpoint accepts a single PUT
        ``/:/prefs?<id>=<value>`` per change — there's no batch form. We
        issue one PUT per ``FlagTarget`` (``library_ids`` is ignored for
        Plex prefs since they're server-wide) and aggregate outcomes
        keyed ``":<flag>"``.
        """
        if not targets:
            return {}

        url = (self._config.plex_url or "").rstrip("/")
        token = self._config.plex_token or ""
        verify_ssl = bool(getattr(self._config, "plex_verify_ssl", True))
        timeout = int(getattr(self._config, "plex_timeout", 10) or 10)
        if not url or not token:
            return {"_global": "Plex URL and token required"}

        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        results: dict[str, str] = {}
        for target in targets:
            pref_id = str(target.get("flag") or "")
            if not pref_id:
                continue
            want = target.get("value")
            if isinstance(want, bool):
                value_str = "true" if want else "false"
            elif isinstance(want, str):
                value_str = want.lower() if want.lower() in ("true", "false") else str(want)
            else:
                value_str = str(want)
            try:
                put = requests.put(
                    f"{url}/:/prefs",
                    params={pref_id: value_str},
                    headers={"X-Plex-Token": token},
                    timeout=timeout,
                    verify=verify_ssl,
                )
                put.raise_for_status()
                results[f":{pref_id}"] = "ok"
            except Exception as exc:
                logger.warning(
                    "Could not update Plex preference {} on server {!r}: {}",
                    pref_id,
                    self.name,
                    exc,
                )
                results[f":{pref_id}"] = f"error: {exc}"
        return results

    def previews_readiness(self) -> dict[str, Any]:
        """Unified readiness payload for the Previews readiness card.

        Returns the envelope documented on
        :meth:`MediaServer.previews_readiness`. Plex sections:
        ``connection``, ``version``, ``library_settings`` (FSEvent
        prefs — server-wide), ``server_config_folder`` (writable
        probe), ``vendor_extraction``, ``path_mappings``.

        Plex has no plugin architecture and no trickplay geometry knob;
        most "checks" are server-wide prefs or filesystem state.
        """
        import os as _os

        sections: list[dict[str, Any]] = []

        # --- Connection + version probe ------------------------------
        connection_result = self.test_connection()
        sections.append(
            {
                "id": "connection",
                "title": "Connection",
                "docs_anchor": "connection",
                "ok": connection_result.ok,
                "severity": "critical",
                "checks": [
                    {
                        "id": "reachable",
                        "label": "Plex reachable",
                        "docs_anchor": "connection",
                        "tooltip": "Server is reachable and returned its machine identifier",
                        "explanation": (
                            "<p><strong>What it checks:</strong> this app sent a GET to the "
                            "Plex root URL and received back the server's "
                            "<code>machineIdentifier</code>, confirming authenticated connectivity.</p>"
                            "<p><strong>Why it matters:</strong> the connection gates every "
                            "other check (server prefs, vendor extraction, path lookups). If "
                            "Plex isn't reachable, the rest of the readiness card can't "
                            "meaningfully report state.</p>"
                            "<p><strong>Common causes when it fails:</strong> wrong URL "
                            "(e.g. <code>localhost</code> from inside this container — use the "
                            "host's IP), expired / invalid Plex token, SSL cert verification "
                            "failing on a self-signed cert, or Plex not running. Read-only "
                            "check — fix the URL/token in the General tab.</p>"
                        ),
                        "ok": connection_result.ok,
                        "severity": "critical",
                        "current": "reachable" if connection_result.ok else "unreachable",
                        "recommended": "reachable",
                        "actions": {},
                        "reason": "" if connection_result.ok else connection_result.message,
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
                        "label": f"Plex {connection_result.version}" if connection_result.version else "Plex version",
                        "docs_anchor": "version",
                        "tooltip": "Informational — any recent Plex release works",
                        "explanation": (
                            "<p><strong>What it reports:</strong> Plex Media Server's "
                            "self-reported version from the root endpoint.</p>"
                            "<p><strong>Why it's informational:</strong> BIF bundle previews "
                            "(how this app publishes to Plex) have been supported for years — "
                            "no minimum-version gate matters for normal operation.</p>"
                            "<p><strong>When it would matter:</strong> if a future Plex release "
                            "ever changes the bundle path layout or BIF format, this row will "
                            "surface the version and recommend an action. Read-only.</p>"
                        ),
                        "ok": True,
                        "severity": "info",
                        "current": connection_result.version or "unknown",
                        "recommended": None,
                        "actions": {},
                        "reason": None,
                        "meta": {},
                    }
                ],
            }
        )

        # --- Library settings (server-wide FSEvent prefs) -----------
        library_checks: list[dict[str, Any]] = []
        library_section_ok = True
        library_severity = "info"

        url = (self._config.plex_url or "").rstrip("/")
        token = self._config.plex_token or ""
        verify_ssl = bool(getattr(self._config, "plex_verify_ssl", True))
        timeout = int(getattr(self._config, "plex_timeout", 10) or 10)
        current_by_id: dict[str, Any] = {}
        if url and token:
            if not verify_ssl:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            try:
                response = requests.get(
                    f"{url}/:/prefs",
                    headers={"X-Plex-Token": token, "Accept": "application/json"},
                    timeout=timeout,
                    verify=verify_ssl,
                )
                response.raise_for_status()
                settings = response.json().get("MediaContainer", {}).get("Setting", [])
                current_by_id = {str(s.get("id") or ""): s.get("value") for s in settings if isinstance(s, dict)}
            except Exception as exc:
                logger.debug("Plex prefs probe failed for {!r}: {}", self.name, exc)

        for pref_id, label, recommended, severity, rationale in self._PLEX_RECOMMENDED_PREFS:
            current = current_by_id.get(pref_id)
            row_ok = current == recommended
            if not row_ok:
                library_section_ok = False
                if severity == "critical":
                    library_severity = "critical"
                elif library_severity != "critical":
                    library_severity = "recommended"
            meta = self._PLEX_FLAG_METADATA.get(pref_id, {})
            library_checks.append(
                {
                    "id": meta.get("check_id", pref_id),
                    "label": label,
                    "docs_anchor": meta.get("docs_anchor", "library-settings"),
                    "tooltip": meta.get("tooltip", rationale),
                    "explanation": meta.get("explanation")
                    or f"<p>{meta.get('tooltip', '') or ''}</p><p>{rationale}</p>",
                    "ok": row_ok,
                    "severity": severity,
                    "current": current,
                    "recommended": recommended,
                    "actions": self._plex_flag_actions(pref_id, current),
                    "reason": None if row_ok else rationale,
                    "meta": {"flag": pref_id},
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

        # --- Plex config folder (filesystem writability) ------------
        # IMPORTANT: pure read-only probe. os.access only — never
        # open(), tempfile, or chmod. The user's Plex config folder
        # might contain hundreds of GB of bundles; we must NEVER risk
        # a stray write that misbehaves.
        plex_config_folder = str(getattr(self._config, "plex_config_folder", "") or "").strip()
        folder_ok = True
        folder_current = "unset"
        folder_reason: str | None = None
        if plex_config_folder:
            exists = _os.path.isdir(plex_config_folder)
            writable = exists and _os.access(plex_config_folder, _os.W_OK)
            if not exists:
                folder_ok = False
                folder_current = "missing"
                folder_reason = f"{plex_config_folder!r} does not exist on this container. Verify the mount is correct."
            elif not writable:
                folder_ok = False
                folder_current = "read-only"
                folder_reason = (
                    f"{plex_config_folder!r} is not writable by this process. "
                    "Check Docker mount (not :ro) and PUID/PGID permissions."
                )
            else:
                folder_current = "writable"
        else:
            folder_ok = False
            folder_reason = "Plex config folder is not configured."
        sections.append(
            {
                "id": "plex_config_folder",
                "title": "Plex config folder",
                "docs_anchor": "plex-config-folder",
                "ok": folder_ok,
                "severity": "critical" if not folder_ok else "info",
                "checks": [
                    {
                        "id": "config_folder_writable",
                        "label": "Plex config folder is writable",
                        "docs_anchor": "plex-config-folder",
                        "tooltip": "Writable path where BIF bundles land",
                        "explanation": (
                            "<p><strong>What it checks:</strong> the <em>Plex config folder</em> "
                            "configured in the General tab exists on this container's filesystem "
                            "AND is writable by the user this app runs as.</p>"
                            "<p><strong>Why it matters:</strong> BIF bundles (the preview files "
                            "Plex reads) land under <code>&lt;config&gt;/Media/localhost/...</code>. "
                            "If the folder is missing, read-only, or mis-mounted, every publish "
                            "fails silently — this app generates the previews but can't write "
                            "them to disk.</p>"
                            "<p><strong>How we probe:</strong> <code>os.access(folder, W_OK)</code> "
                            "only — pure read-only check; we never create, modify, or delete "
                            "anything under your Plex config folder.</p>"
                            "<p><strong>Common causes when it fails:</strong> Docker mount typed "
                            "as <code>:ro</code> instead of read-write, wrong host path, "
                            "PUID/PGID mismatch between this container and Plex's (Plex owns the "
                            "folder and this app's user doesn't have write access). Read-only "
                            "check here — fix via the Docker compose file or General tab.</p>"
                        ),
                        "ok": folder_ok,
                        "severity": "critical" if not folder_ok else "info",
                        "current": folder_current,
                        "recommended": "writable",
                        "actions": {},
                        "reason": folder_reason,
                        "meta": {"path": plex_config_folder},
                    }
                ],
            }
        )

        # --- Vendor-side extraction ---------------------------------
        # Promote this from a single aggregate "stopped on N/M" info row
        # to one actionable row per library, mirroring how Emby and
        # Jellyfin render their per-library trickplay flags. Users with
        # 5 libraries where 2 still have BIF generation on now see two
        # explicit recommended-severity rows with per-library Disable
        # buttons, instead of one info row that hides the offenders.
        vendor_probe_ok = True
        vendor_probe_reason = ""
        try:
            extraction_status = self.get_vendor_extraction_status()
        except Exception as exc:
            logger.debug("Vendor-extraction status probe failed for {!r}: {}", self.name, exc)
            extraction_status = {
                "extracting_count": 0,
                "stopped_count": 0,
                "skipped_count": 0,
                "total": 0,
                "libraries": [],
            }
            vendor_probe_ok = False
            vendor_probe_reason = f"Could not read library extraction state: {exc}"

        vendor_explanation = (
            "<p><strong>What this controls:</strong> Plex's per-library "
            "<code>enableBIFGeneration</code> flag. When on, Plex generates its own "
            "BIF previews during library analysis — the same job this app does.</p>"
            "<p><strong>Why we recommend off:</strong> this app writes BIFs directly into "
            "Plex's bundle directory. If Plex is also generating its own, you end up with "
            "two processes doing the same work — this app's output lands first, Plex's "
            "subsequent pass overwrites it with a lower-quality or differently-spaced "
            "version, undoing everything you just generated.</p>"
            "<p><strong>Custom-agent libraries</strong> (XBMCnfoMovieImporter and similar) "
            "can't be toggled via the Plex API — Plex's section-edit endpoint rejects the "
            "change. Those rows are surfaced as a separate footnote and you have to disable "
            "BIF generation manually in Plex's web UI for them.</p>"
        )

        vendor_checks: list[dict[str, Any]] = []
        if not vendor_probe_ok:
            # Probe failed entirely — surface as recommended (not info)
            # so the frontend's info-filter doesn't drop it and the
            # user actually sees that we couldn't read state. Pre-fix
            # this was severity="info"; the filter dropped it and the
            # vendor section rendered as empty/silent. Severity stays
            # below critical because a missed probe doesn't break
            # preview playback — but the user still needs to know the
            # audit is blind.
            vendor_checks.append(
                {
                    "id": "vendor_extraction_state",
                    "label": "Plex's own BIF generation",
                    "docs_anchor": "vendor-extraction",
                    "tooltip": "Stop Plex generating its own BIF previews",
                    "explanation": vendor_explanation,
                    "ok": False,
                    "severity": "recommended",
                    "current": "unknown (probe failed)",
                    "recommended": "stopped",
                    "actions": {},
                    "reason": vendor_probe_reason or None,
                    "meta": extraction_status,
                }
            )
        else:
            libs = extraction_status.get("libraries") or []
            auditable = [lib for lib in libs if lib.get("state") in ("extracting", "stopped")]
            skipped = [lib for lib in libs if lib.get("state") == "skipped"]

            for lib in auditable:
                section_key = str(lib.get("key") or "")
                section_name = str(lib.get("name") or section_key or "library")
                extracting = lib.get("state") == "extracting"
                vendor_checks.append(
                    {
                        "id": f"vendor_extraction:{section_key}" if section_key else "vendor_extraction_state",
                        "label": f"{section_name} — Plex's BIF generation",
                        "docs_anchor": "vendor-extraction",
                        "tooltip": "Stop Plex generating its own BIF previews on this library",
                        "explanation": vendor_explanation,
                        "ok": not extracting,
                        # Recommended (not info) so the section bubbles up amber
                        # in the new "Recommended" UI bucket and the per-library
                        # offenders stop hiding under an info-coloured banner.
                        "severity": "recommended",
                        "current": bool(extracting),
                        "recommended": False,
                        "actions": {
                            "disable": {
                                "action": "set_vendor_extraction",
                                "args": {"scan_extraction": False, "library_ids": [section_key]}
                                if section_key
                                else {"scan_extraction": False},
                                "confirm": {
                                    "kind": "button",
                                    "phrase": "",
                                    # PLAIN TEXT — the frontend renders this via
                                    # textContent (servers.js:_openConfirmModal),
                                    # so HTML tags would appear as literal markup.
                                    "body": (
                                        f"Stops Plex generating its own BIF previews on "
                                        f"{section_name}. Recommended when this app owns "
                                        "preview generation. Non-destructive — existing "
                                        "bundles stay on disk."
                                    ),
                                },
                            },
                            "enable": {
                                "action": "set_vendor_extraction",
                                "args": {"scan_extraction": True, "library_ids": [section_key]}
                                if section_key
                                else {"scan_extraction": True},
                                "confirm": {
                                    "kind": "button",
                                    "phrase": "",
                                    "body": (
                                        f"Re-enables Plex's own BIF generation on "
                                        f"{section_name}. Plex will generate its own "
                                        "previews in parallel to this app — whichever "
                                        "writes last wins, so app-published previews may "
                                        "get overwritten."
                                    ),
                                },
                            },
                        },
                        "reason": None,
                        "meta": {"library_id": section_key, "library_name": section_name},
                    }
                )

            # Custom-agent libraries can't be toggled via the API. Emit
            # one Manual row per library so each appears in the
            # Recommended bucket as its own to-do — pre-fix this was a
            # single aggregate "1 library/libraries can't be toggled via
            # API" row at info severity, which the frontend's new info
            # filter would drop entirely. Per-library rows match how
            # auditable libraries are rendered (one row each) and let
            # the user check them off in their head as they toggle each
            # one in Plex web UI.
            for lib in skipped:
                section_key = str(lib.get("key") or "")
                section_name = str(lib.get("name") or section_key or "library")
                vendor_checks.append(
                    {
                        "id": f"vendor_extraction_skipped:{section_key}"
                        if section_key
                        else "vendor_extraction_skipped",
                        # Label matches the auditable-row format
                        # exactly — the "Manual fix needed" badge that
                        # the frontend stamps on actionless rows
                        # carries the "this is a manual fix" signal, so
                        # no "(manual)" suffix is needed in the label.
                        "label": f"{section_name} — Plex's BIF generation",
                        "docs_anchor": "vendor-extraction",
                        "tooltip": "Custom-agent library — toggle in Plex web UI",
                        "explanation": vendor_explanation,
                        # ok=False so the row surfaces in the Recommended
                        # bucket; severity=recommended so it doesn't gate
                        # overall_ok (which only critical does).
                        "ok": False,
                        "severity": "recommended",
                        # Bool current/recommended so the side-by-side
                        # diff renders the same "On → Off" pills as the
                        # auditable rows. The "Manual fix needed" badge
                        # (stamped by the frontend when actions is
                        # empty) and the reason text below carry the
                        # manual-fix signal.
                        "current": True,
                        "recommended": False,
                        "actions": {},
                        # Action-first reason so the user reads what to
                        # DO before any technical explanation. The "why
                        # this app can't do it for you" is one short
                        # parenthetical at the end — Plex rejects API
                        # writes for custom-agent libraries, but the
                        # user doesn't need to know that to take
                        # action.
                        "reason": (
                            f"In Plex web UI: Libraries → {section_name} → Edit → Advanced "
                            f'→ untick "Generate video preview thumbnails". '
                            f"(Plex's API rejects this toggle for custom-agent libraries, "
                            f"so it has to be done in the web UI.)"
                        ),
                        "meta": {"library_id": section_key, "library_name": section_name},
                    }
                )

            if not vendor_checks:
                # No libraries at all (fresh install). Keep the section
                # visible so users know what it's for, but mark it clean.
                vendor_checks.append(
                    {
                        "id": "vendor_extraction_state",
                        "label": "Plex's own BIF generation",
                        "docs_anchor": "vendor-extraction",
                        "tooltip": "No libraries to audit yet",
                        "explanation": vendor_explanation,
                        "ok": True,
                        "severity": "info",
                        "current": "no libraries",
                        "recommended": None,
                        "actions": {},
                        "reason": None,
                        "meta": extraction_status,
                    }
                )

        # Section is OK only when every per-library row passes. Keeps
        # severity "recommended" so a row with extraction on amber-flags
        # the section without bumping overall_ok (which is critical-only).
        vendor_section_ok = vendor_probe_ok and all(c.get("ok") for c in vendor_checks)
        # Section severity drives the right-side badge on the section
        # header. "recommended" when ANY row is failing (auditable
        # extracting, manual skipped, or probe-failed) so the section
        # surfaces in the Recommended bucket; "info" only when every
        # row is passing (then frontend hides them anyway).
        if vendor_section_ok:
            vendor_section_severity = "info"
        else:
            vendor_section_severity = "recommended"

        sections.append(
            {
                "id": "vendor_extraction",
                "title": "Vendor-side preview generation",
                "docs_anchor": "vendor-extraction",
                "ok": vendor_section_ok,
                "severity": vendor_section_severity,
                "checks": vendor_checks,
            }
        )

        # --- Path mappings (read-only diagnostic row) ---------------
        mapping_rows = list(getattr(self._config, "path_mappings", None) or [])
        broken: list[str] = []
        for row in mapping_rows:
            if not isinstance(row, dict):
                continue
            local_prefix = str(row.get("local_prefix") or "").strip()
            if local_prefix and not _os.path.isdir(local_prefix):
                broken.append(local_prefix)
        mappings_ok = not broken
        sections.append(
            {
                "id": "path_mappings",
                "title": "Path mappings",
                "docs_anchor": "path-mappings",
                "ok": mappings_ok,
                "severity": "recommended" if not mappings_ok else "info",
                "checks": [
                    {
                        "id": "path_mappings_valid",
                        "label": f"{len(mapping_rows)} path mapping{'s' if len(mapping_rows) != 1 else ''} configured",
                        "docs_anchor": "path-mappings",
                        "tooltip": "Configured mappings must point at paths that exist",
                        "explanation": (
                            "<p><strong>What it checks:</strong> for every configured path "
                            "mapping (Plex server path → container-local path), the local "
                            "prefix actually exists as a directory on this container.</p>"
                            "<p><strong>Why it matters:</strong> path mappings translate between "
                            "the paths Plex reports (e.g. <code>/data/Movies</code> on Plex's "
                            "container) and the paths this app can read (e.g. "
                            "<code>/mnt/media</code> inside ours). A broken local prefix means "
                            "the translation silently no-ops — Plex tells us a file is at "
                            "<code>/data/Movies/X.mkv</code>, we look at the missing "
                            "<code>/mnt/media/Movies/X.mkv</code> that doesn't resolve, and the "
                            "preview never gets generated.</p>"
                            "<p><strong>Common causes when it fails:</strong> Docker volume "
                            "removed or renamed, typo in the mapping, host filesystem "
                            "unmounted. Read-only check — fix via the Path mappings tab in "
                            "this modal.</p>"
                        ),
                        "ok": mappings_ok,
                        "severity": "recommended" if not mappings_ok else "info",
                        "current": len(mapping_rows) - len(broken),
                        "recommended": len(mapping_rows),
                        "actions": {},
                        "reason": (
                            f"{len(broken)} local_prefix path(s) missing on this container: " + ", ".join(broken)
                        )
                        if broken
                        else None,
                        "meta": {"broken": broken},
                    }
                ],
            }
        )

        # ``overall_ok`` gates the big red "action needed" banner on
        # the modal header. Only TRUE blockers should fail it: any
        # check (in any section) with severity=critical AND ok=False.
        # Recommended-severity failures (library_settings FSEvent prefs
        # post-#237, vendor_extraction per-library rows, path mapping
        # warnings) live in the amber Recommended bucket and must not
        # promote the header to red.
        #
        # Pre-#237 this was hard-coded to AND together connection_ok /
        # library_section_ok / folder_ok / mappings_ok. After demoting
        # FSEventLibraryUpdatesEnabled from critical to recommended,
        # library_section_ok could still be False on a row that's only
        # advisory — which spuriously failed overall_ok. The general
        # form here walks the emitted checks, so new severities added
        # later self-resolve without revisiting this line.
        overall_ok = not any(
            (check.get("severity") == "critical" and check.get("ok") is False)
            for section in sections
            for check in (section.get("checks") or [])
        )
        return {
            "vendor": "plex",
            "overall_ok": overall_ok,
            "sections": sections,
        }

    def list_items(self, library_id: str) -> Iterator[MediaItem]:
        """Yield :class:`MediaItem` objects for a single library by id.

        Wraps the per-library scan logic from
        ``plex_client.get_library_sections`` but for a *single* section so the
        publisher-list dispatcher can request items per server.
        """
        from ..plex_client import (
            _build_episode_title,
            _extract_item_locations,
            retry_plex_call,
        )

        plex = self._connect()
        try:
            sections = retry_plex_call(plex.library.sections)
        except Exception as exc:
            logger.warning(
                "Could not list Plex libraries while looking up items for library {}: {}. "
                "Verify Plex is reachable and the access token is valid.",
                library_id,
                exc,
            )
            return

        target = next(
            (s for s in sections if str(getattr(s, "key", "") or "") == str(library_id)),
            None,
        )
        if target is None:
            logger.warning(
                "Plex library with id {} no longer exists on the server — it may have been "
                "deleted or renamed. Open Settings → Media Servers, click 'Refresh libraries' "
                "on this Plex entry, and re-tick the libraries you want to process.",
                library_id,
            )
            return

        try:
            # ``plexapi.LibrarySection.search()`` handles HTTP pagination
            # internally (default container_size=100) and returns the
            # full list once it finishes — so unlike Emby/Jellyfin we
            # can't log per-page progress without forking the search
            # call. Bracket it with two INFO lines so the per-job log
            # has something between the existing "Querying library …"
            # banner and the first item dispatch, instead of a silent
            # 30-120s gap on a large library.
            if target.METADATA_TYPE == "episode":
                logger.info(
                    "Plex library {!r}: requesting full episode list from server "
                    "(plexapi paginates internally; this can take 30-120s for large libraries)…",
                    target.title,
                )
                results = retry_plex_call(target.search, libtype="episode")
                logger.info(
                    "Plex library {!r}: received {} episode(s) from server, starting to yield items.",
                    target.title,
                    len(results),
                )
                for m in results:
                    locations = _extract_item_locations(m)
                    if not locations:
                        continue
                    yield MediaItem(
                        id=_plex_item_id(m),
                        library_id=str(target.key),
                        title=_build_episode_title(m),
                        remote_path=str(locations[0]),
                        bundle_metadata=_extract_plex_bundle_metadata(m),
                    )
            elif target.METADATA_TYPE == "movie":
                logger.info(
                    "Plex library {!r}: requesting full movie list from server "
                    "(plexapi paginates internally; this can take 30-120s for large libraries)…",
                    target.title,
                )
                results = retry_plex_call(target.search)
                logger.info(
                    "Plex library {!r}: received {} movie(s) from server, starting to yield items.",
                    target.title,
                    len(results),
                )
                for m in results:
                    locations = _extract_item_locations(m)
                    if not locations:
                        continue
                    yield MediaItem(
                        id=_plex_item_id(m),
                        library_id=str(target.key),
                        title=str(getattr(m, "title", "") or ""),
                        remote_path=str(locations[0]),
                        bundle_metadata=_extract_plex_bundle_metadata(m),
                    )
            else:
                logger.info(
                    "Skipping Plex library {} (unsupported METADATA_TYPE={})",
                    target.title,
                    target.METADATA_TYPE,
                )
        except Exception as exc:
            logger.warning(
                "Could not list items in Plex library {!r}: {}. "
                "The library may be empty, still scanning, or the Plex server may be busy. "
                "Try again in a few minutes.",
                target.title,
                exc,
            )

    def search_items(self, query: str, limit: int = 50) -> list[MediaItem]:
        """Search Plex via ``searchHubs()`` (cross-library index lookup).

        Pre-fix this called ``library.search(title=needle)`` which is
        scoped to the user's enabled-library config. Live regression
        2026-05-10: a multi-server install with ``plex_library_ids = []``
        (or no library IDs persisted at all) got zero hits across the
        ENTIRE Plex catalogue. The user typed "the matrix" and saw an
        empty list because no per-section was selected for the search
        to walk.

        ``searchHubs()`` is Plex's cross-library hub-search endpoint
        (``/hubs/search``). It always queries every section, which is
        what the Preview Inspector needs — the user's per-server library
        filter is for ingestion, not for browsing. Cost-wise it's
        comparable to a single ``library.search()`` per section but
        without the per-section round-trips. When the parsed query
        carries S##E##, we follow up by drilling into the matched
        Series for the specific episode.

        Empty query → empty list. Failures (auth, network) → empty
        list with a WARNING — the inspector renders an empty result
        rather than spinning.
        """
        from ..plex_client import _build_episode_title, _extract_item_locations, retry_plex_call
        from ..search import SearchQuery
        from ..search.rank import filter_and_rank

        sq = SearchQuery.parse(query)
        if sq.is_empty:
            return []

        plex = self._connect()
        try:
            # searchHubs returns Hub objects (one per type — movies,
            # shows, episodes, actors…); each hub contains items.
            hubs = retry_plex_call(plex.search, query=sq.title, limit=limit)
        except Exception as exc:
            logger.warning(
                "Plex search for {!r} failed ({}: {}). Verify Plex is reachable; falling back to "
                "no-results so the inspector page renders an empty list rather than spinning.",
                sq.raw,
                type(exc).__name__,
                exc,
            )
            return []

        # Project hub items into (name, type, carrier) tuples so the
        # shared rank pass can compare them against the parsed query.
        candidates: list[tuple[str, str, object]] = []
        for m in hubs or []:
            try:
                metadata_type = getattr(m, "METADATA_TYPE", "") or getattr(m, "type", "")
                if metadata_type == "episode":
                    title = _build_episode_title(m)
                else:
                    title = getattr(m, "title", "") or ""
                candidates.append((title, str(metadata_type or "").lower(), m))
            except Exception as exc:
                logger.debug("Skipping Plex search hit due to projection error: {}", exc)
                continue

        ranked = filter_and_rank(sq, candidates, limit=limit * 2)

        items: list[MediaItem] = []

        def _project_to_media_item(plex_obj, metadata_type: str) -> MediaItem | None:
            """Convert a plexapi item into a MediaItem, or None if unprojectable."""
            try:
                locations = _extract_item_locations(plex_obj)
            except Exception:
                locations = []
            if not locations:
                # Show / parent-level matches with no MediaPart of their own —
                # the inspector needs an actual media file.
                return None
            try:
                if metadata_type == "episode":
                    title = _build_episode_title(plex_obj)
                else:
                    title = getattr(plex_obj, "title", "") or ""
                return MediaItem(
                    id=_plex_item_id(plex_obj),
                    title=title,
                    remote_path=locations[0],
                    library_id=str(getattr(plex_obj, "librarySectionID", "") or ""),
                )
            except Exception as exc:
                logger.debug("Skipping Plex search hit due to projection error: {}", exc)
                return None

        for m in ranked:
            if len(items) >= limit:
                break
            # Prefer .type over METADATA_TYPE: plexapi's plex.search() can
            # return Show items whose METADATA_TYPE is "episode" (the type
            # of items inside the search hub) while .type is "show" (the
            # actual object kind). Trusting METADATA_TYPE first leaks Show
            # rows into the episode-projection branch and produces an
            # un-loadable result with the show's title and bundle path
            # rather than expanding into the show's episodes.
            metadata_type = getattr(m, "type", "") or getattr(m, "METADATA_TYPE", "")

            if metadata_type == "show":
                if sq.has_episode:
                    # User typed S##E## — drill straight to the specific episode.
                    try:
                        ep = retry_plex_call(m.episode, season=sq.season, episode=sq.episode)
                    except Exception as exc:
                        logger.debug(
                            "Plex episode lookup S{}E{} on {!r} failed: {}",
                            sq.season,
                            sq.episode,
                            getattr(m, "title", "?"),
                            exc,
                        )
                        continue
                    projected = _project_to_media_item(ep, "episode")
                    if projected is not None:
                        items.append(projected)
                    continue
                # Plain show-name query (no S##E##) — expand into every episode
                # so the inspector lets the user browse the whole show. Mirrors
                # what the legacy /bif/search did for show hubs (api_bif.py:408).
                try:
                    episodes = retry_plex_call(m.episodes)
                except Exception as exc:
                    logger.debug(
                        "Plex episode expansion for {!r} failed: {}",
                        getattr(m, "title", "?"),
                        exc,
                    )
                    continue
                for ep in episodes or []:
                    if len(items) >= limit:
                        break
                    projected = _project_to_media_item(ep, "episode")
                    if projected is not None:
                        items.append(projected)
                continue

            projected = _project_to_media_item(m, str(metadata_type or ""))
            if projected is not None:
                items.append(projected)

        if not items:
            logger.info(
                "[{}] Search returned no results for {!r} (parsed title={!r}, S{}E{})",
                self.name,
                sq.raw,
                sq.title,
                sq.season,
                sq.episode,
            )
        return items

    def resolve_item_to_remote_path(self, item_id: str) -> str | None:
        """Return ``item.media[0].parts[0].file`` for ``item_id``, else ``None``.

        Mirrors the lookup in ``web/webhooks.py:_resolve_plex_paths_from_rating_key``
        but returns a single path (the first usable one) for the abstract
        interface. Returns ``None`` for any failure, by design — the
        dispatcher routes that into the slow-backoff retry queue.
        """
        from ..plex_client import retry_plex_call

        try:
            plex = self._connect()
            item = retry_plex_call(plex.fetchItem, int(item_id))
        except (ValueError, TypeError) as exc:
            logger.debug("Plex item id {!r} is not numeric: {}", item_id, exc)
            return None
        except Exception as exc:
            logger.debug("Plex fetchItem({}) failed: {}", item_id, exc)
            return None

        for media in getattr(item, "media", None) or []:
            for part in getattr(media, "parts", None) or []:
                file_path = getattr(part, "file", None)
                if file_path:
                    return str(file_path)
        return None

    def _resolve_one_path(self, server_view_path: str) -> str | None:
        """Return the Plex ratingKey for the file at ``server_view_path``.

        Uses Plex's per-section ``type=<media_type>&file=<basename>``
        filter — an indexed equality lookup against MediaPart.file
        scoped to a single Plex media type, sub-second on libraries
        of any size. The ``type=`` parameter is REQUIRED: omitting it
        makes Plex return HTTP 500 silently (the legacy
        ``section.all()`` walk this replaced burned 30-90s on large
        libraries because it streamed every item's metadata just to
        filter client-side; the legacy ``_search_by_file_path`` in
        plex_client.py always sent ``type=``, but a copy of the URL
        without it bricks every reverse-lookup. ``type=`` is 1 for
        movies, 4 for episodes — derived from the section's
        ``METADATA_TYPE``.)

        Verifies via the trailing two path components (parent dir +
        basename) so two unrelated files with the same basename don't
        collide. Returns ``None`` when no match exists or the API call
        fails; the dispatcher then routes the publisher to the
        slow-backoff retry queue (the file may not yet be indexed).
        The base class :meth:`MediaServer.resolve_remote_path_to_item_id`
        loops mapped candidates through this hook so callers can pass
        canonical paths.
        """
        import os as _os
        import urllib.parse

        from ..plex_client import _resolve_item_media_type, retry_plex_call

        if not server_view_path:
            return None

        basename = _os.path.basename(server_view_path)
        if not basename:
            return None
        target_tail = "/".join(server_view_path.rstrip("/").split("/")[-2:]).replace("\\", "/")

        try:
            plex = self._connect()
            sections = retry_plex_call(plex.library.sections)
        except Exception as exc:
            logger.debug("Plex reverse-lookup: section enumeration failed: {}", exc)
            return None

        # Plex's file= filter is scoped per (section_key, type_id). Movie
        # libraries use type=1, TV libraries use type=4 (episodes); other
        # METADATA_TYPEs (artist/photo/etc.) don't host video for previews
        # and are skipped.
        type_id_for_section = {
            "movie": 1,
            "episode": 4,
        }
        # Audit L3: respect the user's enabled-library selection. The
        # legacy ``_search_by_file_path`` in plex_client.py honours
        # this; the new resolver must too. Without it, a same-basename
        # collision in a user-disabled library (e.g. a 4K mirror of a
        # movie also in the enabled HD library) returns the disabled
        # library's id → downstream ``server_owns_path`` re-check
        # drops the publisher → silent no-publish for the file the
        # user actually wanted.
        selected_library_ids: set[str] = {
            str(s).strip() for s in (getattr(self._config, "plex_library_ids", None) or []) if str(s).strip()
        }
        selected_library_titles: set[str] = {
            str(n).strip().lower() for n in (getattr(self._config, "plex_libraries", None) or []) if str(n).strip()
        }

        def _is_selected(section) -> bool:
            if selected_library_ids:
                return str(getattr(section, "key", "")).strip() in selected_library_ids
            if selected_library_titles:
                return str(getattr(section, "title", "")).strip().lower() in selected_library_titles
            return True

        for section in sections:
            if not _is_selected(section):
                continue
            section_key = getattr(section, "key", None)
            if section_key is None:
                continue
            media_type = _resolve_item_media_type(getattr(section, "METADATA_TYPE", ""))
            type_id = type_id_for_section.get(media_type or "")
            if type_id is None:
                continue
            ekey = f"/library/sections/{section_key}/all?type={type_id}&file={urllib.parse.quote(basename)}"
            try:
                items = retry_plex_call(plex.fetchItems, ekey)
            except Exception as exc:
                logger.debug(
                    "Plex reverse-lookup: file= query failed for {!r} in section {}: {}",
                    basename,
                    section_key,
                    exc,
                )
                continue
            for item in items:
                for media in getattr(item, "media", None) or []:
                    for part in getattr(media, "parts", None) or []:
                        file_path = str(getattr(part, "file", None) or "").replace("\\", "/")
                        if not file_path:
                            continue
                        if _os.path.basename(file_path) == basename and file_path.endswith(target_tail):
                            rating_key = getattr(item, "ratingKey", None)
                            if rating_key is not None:
                                return str(rating_key)
        return None

    def _trigger_path_refresh(self, server_view_path: str) -> None:
        """Trigger a partial Plex library scan for one server-view path.

        Plex's targeted-scan endpoint accepts a folder path within a
        library section. We pass ``path_mappings=None`` so the helper
        does not re-expand candidates — the base class
        :meth:`MediaServer.trigger_refresh` has already looped every
        mapped candidate through this hook.
        """
        from ..plex_client import trigger_plex_partial_scan

        trigger_plex_partial_scan(
            plex_url=self._config.plex_url,
            plex_token=self._config.plex_token,
            unresolved_paths=[server_view_path],
            path_mappings=None,
            verify_ssl=bool(getattr(self._config, "plex_verify_ssl", True)),
            server_display_name=getattr(self._config, "server_display_name", None) or self.name,
        )

    def get_bundle_metadata(self, item_id: str) -> list[tuple[str, str]]:
        """Return ``(bundle_hash, remote_path)`` for every MediaPart of an item.

        Plex-specific helper (not part of the abstract :class:`MediaServer`
        interface) used by :class:`PlexBundleAdapter` to compute the BIF output
        location. Plex's ``/library/metadata/{id}/tree`` endpoint returns XML;
        we surface the relevant attributes as plain tuples.

        ``item_id`` may be either a bare ratingKey (``"557676"``) or a full
        Plex API path (``"/library/metadata/557676"``); we normalise both so
        a caller that accidentally passes the URL form doesn't end up
        querying ``/library/metadata//library/metadata/557676/tree`` (404,
        previously misreported as ``not_indexed``). The path-form input
        used to be the silent root cause of every Sonarr/Radarr → Plex
        webhook returning ``skipped_not_indexed`` — see D31.

        Returns an empty list when the lookup fails or the item has no parts —
        the adapter translates that into a
        :class:`~media_preview_generator.servers.LibraryNotYetIndexedError`.
        Failures now WARN (not DEBUG) so the next time we malform a URL it
        shows up in logs without users having to grep at debug level.
        """
        from ..plex_client import retry_plex_call

        # D31 — accept either bare ratingKey or full /library/metadata/<id>
        # form. Strip any prefix segments so the f-string below can't double.
        item_id_str = str(item_id or "").strip()
        bare_id = item_id_str.rsplit("/", 1)[-1] if item_id_str else ""
        if not bare_id:
            logger.warning("Plex /tree called with empty item_id; cannot compute bundle hash")
            return []

        try:
            data = retry_plex_call(self._connect().query, f"/library/metadata/{bare_id}/tree")
        except Exception as exc:
            logger.warning(
                "Plex /tree query failed for item {!r} ({}: {}). The publisher "
                "will be reported as 'not indexed yet' and retried, but the underlying "
                "cause is this query — not Plex's analyzer.",
                bare_id,
                type(exc).__name__,
                exc,
            )
            return []

        results: list[tuple[str, str]] = []
        for part in data.findall(".//MediaPart"):
            bundle_hash = part.attrib.get("hash") or ""
            file_path = part.attrib.get("file") or ""
            if bundle_hash and file_path:
                results.append((bundle_hash, file_path))
        return results

    def parse_webhook(self, payload: dict[str, Any] | bytes, headers: dict[str, str]) -> WebhookEvent | None:
        """Normalise a Plex webhook payload to a :class:`WebhookEvent`.

        Plex sends multipart form-data with a JSON ``payload`` field. This
        method accepts either the parsed JSON ``dict`` (when an upstream
        layer already extracted it) or raw ``bytes`` for the multipart body.

        Returns ``None`` when the event is not relevant to BIF generation
        (e.g. ``media.play``, ``media.pause``).
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

        event_type = str(data.get("event") or "")
        # Only library.new is interesting for BIF generation; the others all
        # describe playback state which is irrelevant here.
        if event_type != "library.new":
            return None

        metadata = data.get("Metadata") or {}
        rating_key = metadata.get("ratingKey")
        item_id = str(rating_key) if rating_key not in (None, "") else None

        return WebhookEvent(event_type=event_type, item_id=item_id, raw=data)
