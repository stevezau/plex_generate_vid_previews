"""Plex implementation of the :class:`MediaServer` interface.

This is a thin façade over the existing :mod:`media_preview_generator.plex_client`
helpers so the rest of the codebase can be migrated to the abstract interface
without rewriting Plex-specific logic. As the multi-server refactor lands, the
inline calls in :mod:`processing.generator` and :mod:`web.webhooks` are
re-routed through this class.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import requests
import urllib3
from loguru import logger

from .base import (
    ConnectionResult,
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

    @property
    def type(self) -> ServerType:
        return ServerType.PLEX

    @property
    def config(self) -> Config:
        """Expose the wrapped :class:`Config` for transitional callers."""
        return self._config

    def _connect(self):
        """Lazily create the underlying ``plexapi`` server connection."""
        from ..plex_client import plex_server as _build_plex

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
        # plexapi maps the field's allowed values to int 0/1 (bool).
        # Passing the string "0"/"1" raises "0 not found in {0: False, 1: True}".
        value = 1 if scan_extraction else 0
        for section in sections:
            section_key = str(getattr(section, "key", "") or "")
            if not section_key:
                continue
            if target is not None and section_key not in target:
                continue
            try:
                section.editAdvanced(enableBIFGeneration=value)
                results[section_key] = "ok"
            except Exception as exc:
                # Custom-agent libraries (Sportarr / XBMCnfoMovieImporter
                # / community agents) hit a 400: Plex's section edit
                # endpoint validates the agent against its built-in
                # registry. There's no API path to bypass that — the
                # user has to flip the toggle in Plex's web UI for
                # those libraries. Report distinctly so the user knows
                # WHY a library was skipped, not just "error".
                msg = str(exc)
                if "agent" in msg.lower() and "400" in msg:
                    results[section_key] = "skipped: custom agent (toggle manually in Plex UI)"
                    logger.info(
                        "Plex library {} on server {!r} uses a custom agent — Plex's edit API doesn't accept "
                        "BIF-generation toggle for custom agents. Disable it manually in Plex (Library → Edit → Advanced).",
                        section_key,
                        self.name,
                    )
                else:
                    logger.warning(
                        "Could not update Plex library {} BIF-generation preference on server {!r}: {}",
                        section_key,
                        self.name,
                        exc,
                    )
                    results[section_key] = f"error: {exc}"
        return results

    def get_vendor_extraction_status(self) -> dict[str, int]:
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
        """
        from ..plex_client import retry_plex_call

        try:
            sections = retry_plex_call(self._connect().library.sections)
        except Exception as exc:
            logger.debug("Vendor-extraction status probe failed for {!r}: {}", self.name, exc)
            return {"extracting_count": 0, "stopped_count": 0, "skipped_count": 0, "total": 0}

        extracting = stopped = skipped = 0
        for section in sections:
            try:
                # plexapi exposes per-section settings (the "Advanced"
                # tab in Plex web UI) via ``section.settings()`` —
                # NOT ``section.advanced``/``section.preferences`` which
                # don't exist. The relevant entry is enableBIFGeneration
                # (a Bool-typed Setting). Custom-agent libraries can
                # raise on the call; count those as ``skipped`` so the
                # UI shows the same footnote ``set_vendor_extraction``
                # surfaces for them.
                settings = retry_plex_call(section.settings)
                bif_setting = next(
                    (s for s in settings if str(getattr(s, "id", "")) == "enableBIFGeneration"),
                    None,
                )
                if bif_setting is None:
                    skipped += 1
                    continue
                # Setting.value is True/False for bool settings.
                if bool(getattr(bif_setting, "value", False)):
                    extracting += 1
                else:
                    stopped += 1
            except Exception as exc:
                logger.debug(
                    "Could not audit Plex section {} BIF-generation on {!r}: {}",
                    getattr(section, "key", "?"),
                    self.name,
                    exc,
                )
                skipped += 1

        return {
            "extracting_count": extracting,
            "stopped_count": stopped,
            "skipped_count": skipped,
            "total": extracting + stopped + skipped,
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
            "critical",
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
            if target.METADATA_TYPE == "episode":
                results = retry_plex_call(target.search, libtype="episode")
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
                for m in retry_plex_call(target.search):
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
        """Search Plex via ``library.search()`` (server-side index lookup).

        Plex's native search API returns matches across all sections in a
        single round-trip. The base-class default would walk every item in
        every library — D4 measured 13.6s for a single-word query against
        a 119k-item Plex; this override drops that to <1s by letting Plex
        do the filtering itself.
        """
        from ..plex_client import _build_episode_title, _extract_item_locations, retry_plex_call

        needle = (query or "").strip()
        if not needle:
            return []
        plex = self._connect()
        try:
            raw_results = retry_plex_call(plex.library.search, title=needle, limit=limit)
        except Exception as exc:
            logger.warning(
                "Plex search for {!r} failed ({}: {}). Verify Plex is reachable; falling back to "
                "no-results so the inspector page renders an empty list rather than spinning.",
                needle,
                type(exc).__name__,
                exc,
            )
            return []

        items: list[MediaItem] = []
        for m in raw_results or []:
            if len(items) >= limit:
                break
            try:
                locations = _extract_item_locations(m)
            except Exception:
                locations = []
            if not locations:
                # Plex may return parent-level matches (a Show row whose
                # individual Episodes carry the file paths) — skip those
                # since the inspector needs an actual media file to load
                # the BIF.
                continue
            try:
                metadata_type = getattr(m, "METADATA_TYPE", "") or getattr(m, "type", "")
                if metadata_type == "episode":
                    title = _build_episode_title(m)
                else:
                    title = getattr(m, "title", "") or ""
                items.append(
                    MediaItem(
                        id=_plex_item_id(m),
                        title=title,
                        type=metadata_type or "",
                        remote_path=locations[0] if locations else "",
                        library_id=str(getattr(m, "librarySectionID", "") or ""),
                    )
                )
            except Exception as exc:
                logger.debug("Skipping Plex search hit due to projection error: {}", exc)
                continue
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

        Uses Plex's per-section ``file=<basename>`` filter
        (``/library/sections/{key}/all?file=<basename>``) — an indexed
        equality lookup against MediaPart.file, sub-second on libraries
        of any size. The legacy ``section.all()`` walk this replaced
        burned 30-90s on large libraries because it streamed every
        item's metadata just to filter client-side.

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

        from ..plex_client import retry_plex_call

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

        for section in sections:
            section_key = getattr(section, "key", None)
            if section_key is None:
                continue
            ekey = f"/library/sections/{section_key}/all?file={urllib.parse.quote(basename)}"
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
