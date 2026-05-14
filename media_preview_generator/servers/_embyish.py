"""Shared base for Emby + Jellyfin clients.

Jellyfin forked from Emby; for the endpoints this tool touches the
REST surface is nearly identical (``/System/Info``, ``/Library/VirtualFolders``,
``/Items``, ``/Items/{id}``). The two concrete clients share ~90% of
their implementation. This base class hosts the common logic so each
subclass needs to specify only:

* :attr:`type` — the :class:`ServerType` enum value.
* :attr:`vendor_name` — the vendor brand for log strings.
* :meth:`trigger_refresh` — Emby has a path-based endpoint Jellyfin doesn't.
* :meth:`parse_webhook` — payload shapes differ.
"""

from __future__ import annotations

import os
import re
import threading
import time
import unicodedata
from collections.abc import Iterator
from typing import Any

import requests
import urllib3
from loguru import logger

from .base import (
    ConnectionResult,
    Library,
    MediaItem,
    MediaServer,
    ServerConfig,
)

# How long the per-instance reverse-lookup cache holds a result before
# re-querying the server. 5 min is a generous floor — the only way the
# answer becomes wrong inside the window is the server gaining/losing
# the file, which is exactly what the SKIPPED_NOT_IN_LIBRARY scan-nudge
# + retry queue (D32) is built to handle on a follow-up dispatch. Cache
# eats the 30s Pass-2 enumeration cost on repeat lookups within the
# window — matters most for full-library scans where the same server
# is asked about 200+ files in quick succession (job 818c42b8 ran 418
# such calls in 20 minutes, ~5.3s/item average; with this cache the
# repeat calls collapse to ~0ms).
_REVERSE_LOOKUP_TTL_S = 300.0

# Page size for full-library item enumeration. Jellyfin/Emby honour the
# ``Limit`` query param on ``/Items`` and silently truncate above it —
# pre-fix we sent ``Limit=5000`` once with no ``StartIndex`` follow-up
# and capped every >5000-item library to 5000 (job 9eb79d9c reported
# ``0/5000`` on a multi-thousand-item movies library). 1000 matches
# the reverse-lookup Pass-2 enumerate cap below and keeps single-page
# round-trips small enough that a transient HTTP failure is cheap to
# retry. Loop terminates on a short page (or ``TotalRecordCount``
# exhausted) so libraries of any size enumerate fully.
_LIST_ITEMS_PAGE_SIZE = 1000

# Retry / backoff for transient ``/Items`` page failures during
# ``list_items``. Job b6deeac3 reproducer: Jellyfin took >30s to
# answer the first /Items query on a 118k-item Shows library
# (transient — the same query ran in 0.2s minutes later), the
# request timed out, and the library was silently skipped. With
# 3 attempts and a 2s base wait between, the next two retries
# almost always catch the server once it's idle again. Tuned
# conservatively because every retry costs a fresh HTTP round-trip
# against a Jellyfin server that's already shown signs of stress.
_LIST_ITEMS_MAX_ATTEMPTS = 3
_LIST_ITEMS_RETRY_BASE_WAIT_S = 2.0

# Per-request timeout for the ``/Items`` page query. The default
# ``_request`` timeout (30s) is fine for small API calls but cold-
# cache queries on huge libraries (118k items on Jellyfin Shows)
# blow past it because Jellyfin walks the entire library before
# returning the first row. Empirically the cold walk completes in
# ~10-30s on a healthy server; 60s gives us margin for a server
# that's also doing its own indexing in the background. Combined
# with the 3-attempt retry budget the ceiling is ``3 × 60s + 2s
# + 4s`` ≈ ~186s before ``list_items`` gives up on a page.
_LIST_ITEMS_TIMEOUT_S = 60

# Issue #237: ``/Library/VirtualFolders`` includes music, photo, books
# and audiobook libraries alongside the video ones. None of those have
# a preview-thumbnail toggle (Emby/Jellyfin don't generate previews for
# non-video media), so the readiness card must skip them entirely —
# otherwise users see a "Music — Skip Emby's own trickplay generation"
# row that points at a setting the host UI doesn't expose.
#
# Conservative blacklist: known non-video CollectionType values only.
# Anything else (including missing/None, or unknown values like a new
# Emby release inventing a video collection type) keeps generating
# rows so we don't silently hide legitimate video libraries.
_NON_VIDEO_COLLECTION_TYPES: frozenset[str] = frozenset({"music", "musicvideos", "photos", "books", "audiobooks"})


def is_video_library_folder(raw: dict) -> bool:
    """Return True when an Emby/Jellyfin VirtualFolder dict should be
    treated as a video library for readiness / preview purposes.

    See :data:`_NON_VIDEO_COLLECTION_TYPES` for the blacklist; anything
    not in that set (including missing/None) is considered video and
    keeps generating readiness rows.
    """
    if not isinstance(raw, dict):
        return False
    collection_type = str(raw.get("CollectionType") or "").lower()
    return collection_type not in _NON_VIDEO_COLLECTION_TYPES


class EmbyApiClient(MediaServer):
    """Base class for Emby and Jellyfin clients.

    Concrete subclasses set ``vendor_name`` (used in log strings) and
    override the bits that genuinely differ between the two vendors —
    ``trigger_refresh`` and ``parse_webhook``.
    """

    #: Display brand used in log lines (e.g. "Emby", "Jellyfin").
    vendor_name: str = "Media server"

    def __init__(self, config: ServerConfig, *, default_name: str | None = None) -> None:
        super().__init__(server_id=config.id, name=config.name or (default_name or self.vendor_name))
        self._config = config
        # Persistent requests.Session for HTTP keep-alive across the dozens of
        # /Items + /Library/VirtualFolders + /Items/{id} round-trips a single
        # full-library scan makes. Without a Session, every call paid the TCP
        # handshake + TLS negotiation tax — a 500-item scan amortised that out
        # to seconds of wasted wall time on top of the actual API latency.
        self._session: requests.Session | None = None
        # Double-checked-locking guard for ``_get_session``. Without
        # this, N parallel workers cold-hitting the same client all
        # see ``self._session is None`` and create their own Session
        # objects — N-1 immediately leak to GC. Cheaper to leak than
        # PlexServer's TLS handshakes but still wrong, and the lock
        # is also virtually free on the hot path (single attribute
        # read, no acquire) thanks to double-checked locking.
        self._session_lock = threading.Lock()
        # Reverse-lookup cache: ``{remote_path: (expires_at, item_id)}``.
        # Caches POSITIVE results only — see ``_resolve_one_path`` for the
        # negative-cache regression (chain ``62e32c35``, 2026-05-11) that
        # forced every early retry to short-circuit on stale ``None`` for
        # the full TTL. Value type stays ``str | None`` for backwards-compat
        # with any in-memory entry written by older code paths; the reader
        # in ``_resolve_one_path`` defensively ignores ``None`` values.
        self._reverse_lookup_cache: dict[str, tuple[float, str | None]] = {}
        self._reverse_lookup_lock = threading.Lock()

    @property
    def config(self) -> ServerConfig:
        return self._config

    def _get_session(self) -> requests.Session:
        """Return the lazy-init requests.Session for this client.

        Thread-safe via double-checked locking — see comment on
        ``self._session_lock``. Hot path is a single attribute read
        with no lock acquisition; cold path acquires the lock and
        re-checks before constructing.
        """
        session = self._session
        if session is not None:
            return session
        with self._session_lock:
            if self._session is None:
                self._session = requests.Session()
        return self._session

    # ------------------------------------------------------------------ HTTP
    def _token(self) -> str:
        """Extract the X-Emby-Token value from the persisted auth dict.

        Both vendors accept either ``access_token`` (from the
        password / Quick Connect flow) or ``api_key`` (paste-in) on
        the legacy ``X-Emby-Token`` header.
        """
        auth = self._config.auth or {}
        return str(auth.get("access_token") or auth.get("api_key") or auth.get("token") or "")

    def _user_id(self) -> str | None:
        auth = self._config.auth or {}
        user_id = auth.get("user_id")
        return str(user_id) if user_id else None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        timeout: int | float | None = None,
    ) -> requests.Response:
        """Issue an authenticated request against the server's HTTP API.

        ``timeout`` overrides the per-server default (``self._config.timeout``
        or 30s) for endpoints where the default is too short. Used by
        ``list_items`` for ``/Items`` page queries on large libraries
        where Jellyfin's cold-cache walk can take >30s — see
        ``_LIST_ITEMS_TIMEOUT_S``.
        """
        url = f"{self._config.url.rstrip('/')}{path}"
        headers = {
            "X-Emby-Token": self._token(),
            "Accept": "application/json",
        }
        verify = bool(self._config.verify_ssl)
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        effective_timeout = int(timeout) if timeout is not None else int(self._config.timeout or 30)
        return self._get_session().request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=effective_timeout,
            verify=verify,
        )

    # ------------------------------------------------ Scheduled tasks
    # Jellyfin's ``RefreshTrickplayImages`` task (Emby has an equivalent
    # under the same Key on newer versions) runs daily by default. It
    # scans every video in enabled libraries and generates trickplay
    # tiles via FFmpeg for files that don't have any. For users running
    # the Media Preview Bridge plugin, the registration path is already
    # instant (the plugin writes the trickplay DB row directly when
    # this app publishes), so the scheduled task is mostly redundant
    # CPU and IO. For users WITHOUT the plugin, the scheduled task is
    # how Jellyfin eventually discovers and registers existing tile
    # files — disabling it there breaks trickplay silently.
    #
    # The probe + setter live here because the API surface is identical
    # on both Emby and Jellyfin (both inherit ``EmbyApiClient``).
    _TRICKPLAY_TASK_KEY = "RefreshTrickplayImages"

    def get_scheduled_trickplay_state(self) -> dict[str, Any]:
        """Probe the server's scheduled-trickplay task.

        Returns a dict shape consumed by the readiness card:

          * ``found`` — True when the task exists on this server version.
          * ``task_id`` — opaque server-side id (needed to POST the
            triggers update). Empty when ``found=False``.
          * ``triggers_count`` — number of triggers currently attached.
            >0 means the task auto-runs; 0 means it only fires when a
            human clicks "Run" in the dashboard.
          * ``state`` — Idle / Running / Cancelled (server-reported).
          * ``description`` — server-emitted description so the readiness
            row can quote the server's own copy if needed.
          * ``error`` — short reason when the probe failed (transport
            error / 404 / parse error). Empty on success.

        Tolerant of every failure mode — the readiness card has to render
        even when this single probe fails.
        """
        try:
            response = self._request("GET", "/ScheduledTasks", params={"IsHidden": "false"})
        except Exception as exc:
            return {
                "found": False,
                "task_id": "",
                "triggers_count": 0,
                "state": "",
                "description": "",
                "error": f"{type(exc).__name__}: {exc}"[:200],
            }
        if response.status_code != 200:
            return {
                "found": False,
                "task_id": "",
                "triggers_count": 0,
                "state": "",
                "description": "",
                "error": f"HTTP {response.status_code}",
            }
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            return {
                "found": False,
                "task_id": "",
                "triggers_count": 0,
                "state": "",
                "description": "",
                "error": f"bad JSON: {exc}"[:200],
            }
        if not isinstance(payload, list):
            return {
                "found": False,
                "task_id": "",
                "triggers_count": 0,
                "state": "",
                "description": "",
                "error": "unexpected /ScheduledTasks shape",
            }
        for task in payload:
            if not isinstance(task, dict):
                continue
            if task.get("Key") != self._TRICKPLAY_TASK_KEY:
                continue
            triggers = task.get("Triggers") or []
            return {
                "found": True,
                "task_id": str(task.get("Id") or ""),
                "triggers_count": len(triggers) if isinstance(triggers, list) else 0,
                "state": str(task.get("State") or ""),
                "description": str(task.get("Description") or ""),
                "error": "",
            }
        # Task not present on this server (older Emby version, or feature
        # disabled by build flag) — not an error, just nothing to flag.
        return {
            "found": False,
            "task_id": "",
            "triggers_count": 0,
            "state": "",
            "description": "",
            "error": "",
        }

    def set_scheduled_trickplay_triggers(self, *, enabled: bool) -> dict[str, Any]:
        """Toggle the scheduled-trickplay task.

        ``enabled=False`` clears all triggers (the task can still be
        manually invoked but won't auto-run). ``enabled=True`` restores
        the vendor default of one Daily trigger at 03:00 — matches the
        out-of-the-box behaviour so users who flip-flop don't end up
        with a permanently-armed-but-no-trigger task.

        Returns ``{"ok": bool, "error": str}``. Best-effort — the caller
        (the readiness route) surfaces failures via the standard UI.
        """
        state = self.get_scheduled_trickplay_state()
        if not state.get("found"):
            return {
                "ok": False,
                "error": state.get("error") or "scheduled trickplay task not present on this server",
            }
        task_id = state["task_id"]
        if not task_id:
            return {"ok": False, "error": "scheduled task id missing from probe"}
        # 03:00 in .NET DateTime ticks (one tick = 100 ns; 3 hours
        # = 3 × 3600 × 10^7 = 108_000_000_000). Matches the default
        # the server ships with, so a user who hits Disable then Enable
        # gets back to where they started.
        triggers = [] if not enabled else [{"Type": "DailyTrigger", "TimeOfDayTicks": 108_000_000_000}]
        try:
            response = self._request(
                "POST",
                f"/ScheduledTasks/{task_id}/Triggers",
                json_body=triggers,
            )
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
        if response.status_code not in (200, 204):
            return {
                "ok": False,
                "error": f"HTTP {response.status_code}: {response.text[:200] if response.text else ''}",
            }
        return {"ok": True, "error": ""}

    # ------------------------------------------------ Public query helpers
    def query_items(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Run a parameterised ``GET /Items`` query and return the ``Items`` list.

        Public counterpart to the private :meth:`_request` so callers in the
        :mod:`processing` package don't need to reach across module boundaries
        for the recently-added scan (``SortBy=DateCreated``) or any other
        ad-hoc Items-endpoint query both Emby and Jellyfin share.

        Returns the parsed ``Items`` array (empty list on transport failure;
        the failure is logged so the caller can stay quiet on routine
        outages without losing diagnostic signal).
        """
        try:
            response = self._request("GET", "/Items", params=params)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 — protocol contract is "empty list on failure"
            logger.warning(
                "Could not query /Items on {} server {!r} ({}: {}). "
                "Returning empty list — verify the server is reachable and the token is valid.",
                self.vendor_name,
                self._config.name or self._config.id,
                type(exc).__name__,
                exc,
            )
            return []
        items = payload.get("Items") if isinstance(payload, dict) else None
        return items if isinstance(items, list) else []

    # ------------------------------------------------------------ MediaServer
    def test_connection(self) -> ConnectionResult:
        """Probe ``/System/Info`` for identity and credential validation."""
        if not self._config.url:
            return ConnectionResult(ok=False, message=f"{self.vendor_name} URL is required")
        if not self._token():
            return ConnectionResult(ok=False, message=f"{self.vendor_name} access token / API key is required")

        try:
            response = self._request("GET", "/System/Info")
            response.raise_for_status()
            data = response.json()
            return ConnectionResult(
                ok=True,
                server_id=str(data.get("Id") or "") or None,
                server_name=str(data.get("ServerName") or "") or None,
                version=str(data.get("Version") or "") or None,
                message="Connected",
            )
        except requests.exceptions.SSLError as exc:
            return ConnectionResult(ok=False, message=f"SSL certificate verification failed: {exc}")
        except requests.exceptions.Timeout:
            return ConnectionResult(
                ok=False,
                message=f"Connection to {self._config.url} timed out",
            )
        except requests.exceptions.ConnectionError as exc:
            return ConnectionResult(
                ok=False,
                message=f"Could not connect to {self.vendor_name} at {self._config.url}: {exc}",
            )
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 401:
                msg = f"{self.vendor_name} rejected the access token (401)"
            elif status == 403:
                msg = f"Access denied by {self.vendor_name} server (403)"
            else:
                msg = f"{self.vendor_name} returned HTTP {status}"
            return ConnectionResult(ok=False, message=msg)
        except (ValueError, requests.RequestException) as exc:
            return ConnectionResult(ok=False, message=f"Connection test failed: {exc}")

    def list_libraries(self) -> list[Library]:
        """List "Virtual Folders" with their folder paths.

        Both vendors expose ``/Library/VirtualFolders`` returning each
        library's name, id, and one or more ``Locations`` (server-side
        paths). Per-library ``enabled`` is sourced from the existing
        snapshot in ``self._config.libraries`` so the user's toggle
        survives a refresh.
        """
        try:
            response = self._request("GET", "/Library/VirtualFolders")
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning(
                "Could not fetch the library list from {} server {!r}: {}. "
                "Verify the server is running and your access token / API key in "
                "Settings → Media Servers is still valid.",
                self.vendor_name,
                self.name,
                exc,
            )
            return []

        if not isinstance(data, list):
            logger.warning(
                "{} server {!r} returned an unexpected library list — the response wasn't in the format we expected. "
                "This usually means the server is misconfigured or running a version this app doesn't support. "
                "Library scanning is paused for this server; other servers continue normally. "
                "Check the server's version, then restart it and try again.",
                self.vendor_name,
                self.name,
            )
            return []

        existing_enabled = {lib.id: lib.enabled for lib in self._config.libraries}
        libraries: list[Library] = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            lib_id = str(raw.get("ItemId") or raw.get("Id") or raw.get("Name") or "")
            name = str(raw.get("Name") or "")
            locations = tuple(str(loc) for loc in (raw.get("Locations") or []))
            kind = str(raw.get("CollectionType") or "") or None
            enabled = existing_enabled.get(lib_id, True)
            libraries.append(
                Library(
                    id=lib_id,
                    name=name,
                    remote_paths=locations,
                    enabled=enabled,
                    kind=kind,
                )
            )
        return libraries

    def list_items(self, library_id: str) -> Iterator[MediaItem]:
        """Yield every video :class:`MediaItem` inside the given library.

        Pages through ``/Items`` via ``StartIndex`` until exhausted —
        Jellyfin/Emby silently truncate at ``Limit``, so a single
        un-paginated request capped multi-thousand-item libraries
        (job 9eb79d9c hit this on a >5000-item Jellyfin movies library).
        Terminates when a page returns fewer than ``_LIST_ITEMS_PAGE_SIZE``
        items, or when ``StartIndex`` reaches the server-reported
        ``TotalRecordCount`` (when present) — whichever is sooner.

        First-page failure: log a warning and return cleanly. The caller
        in ``_shared.py:list_canonical_paths`` already treats this as a
        whole-library outage (skip this library, continue scan).

        Mid-pagination failure (``start_index > 0``): re-raise. If we
        swallowed it, the caller would treat a partial enumeration
        (pages 1..N-1) as a complete library — silently dropping every
        item past the failed page. That's the same shape of bug the
        ``Limit=5000`` cap caused before this fix. ``_enumerate_items_for_servers``
        already wraps the iteration in ``try/except`` and logs a
        server-level WARNING, so re-raising produces a visible
        "this server's enumeration partially failed" signal without
        crashing the whole job. Items already yielded keep flowing —
        they're real and need processing; the re-raise only flags that
        the **library is incomplete**.
        """
        start_index = 0
        while True:
            params = {
                "ParentId": library_id,
                "IncludeItemTypes": "Movie,Episode",
                "Recursive": "true",
                "Fields": "Path",
                "Limit": _LIST_ITEMS_PAGE_SIZE,
                "StartIndex": start_index,
            }
            payload = None
            last_exc: Exception | None = None
            for attempt in range(1, _LIST_ITEMS_MAX_ATTEMPTS + 1):
                try:
                    # Pass the extended timeout on EVERY attempt — a
                    # regression that drops the override on retries
                    # would silently revert to the 30s default and
                    # re-introduce the b5651c8a symptom.
                    response = self._request(
                        "GET",
                        "/Items",
                        params=params,
                        timeout=_LIST_ITEMS_TIMEOUT_S,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt >= _LIST_ITEMS_MAX_ATTEMPTS:
                        break
                    # Exponential backoff between attempts. Logging at
                    # INFO so the per-job log shows the recovery
                    # narrative (timeout → wait → retry) instead of a
                    # mystery delay between two WARNING / ERROR lines.
                    wait = _LIST_ITEMS_RETRY_BASE_WAIT_S * (2 ** (attempt - 1))
                    logger.info(
                        "{} /Items page (library={}, StartIndex={}) attempt {}/{} failed "
                        "({}: {}); retrying in {:.1f}s.",
                        self.vendor_name,
                        library_id,
                        start_index,
                        attempt,
                        _LIST_ITEMS_MAX_ATTEMPTS,
                        type(exc).__name__,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
            if payload is None:
                exc = last_exc if last_exc is not None else RuntimeError("exhausted retries with no exception captured")
                if start_index == 0:
                    logger.warning(
                        "Could not list items in {} library {} (first page exhausted "
                        "{} attempts, last error {}: {}). This library will be skipped "
                        "for this run — verify the server is reachable, the API key / "
                        "token is still valid, and that the library hasn't been deleted "
                        "on the server side.",
                        self.vendor_name,
                        library_id,
                        _LIST_ITEMS_MAX_ATTEMPTS,
                        type(exc).__name__,
                        exc,
                    )
                    return
                logger.error(
                    "Partial enumeration of {} library {}: page starting at StartIndex={} "
                    "exhausted {} attempts (last error {}: {}). {} item(s) already yielded; "
                    "re-raising so the caller treats this library as INCOMPLETE rather than "
                    "silently dropping every item past the failed page. Re-run the scan once "
                    "the server is healthy to pick up the rest.",
                    self.vendor_name,
                    library_id,
                    start_index,
                    _LIST_ITEMS_MAX_ATTEMPTS,
                    type(exc).__name__,
                    exc,
                    start_index,
                )
                raise exc

            raw_items = payload.get("Items", []) or []
            # Per-page progress log so the per-job log shows steady
            # pagination progress instead of a 30s+ silent gap between
            # "Querying library …" and "Found N item(s)". For a 118k
            # Shows library at 1000/page this produces ~119 lines over
            # the enumeration phase — paced at the network round-trip
            # rate (so 1-10 lines/sec). Includes ``TotalRecordCount``
            # when the server returned one so the user can see "page
            # 5/119" not just "page 5 of unknown".
            total = payload.get("TotalRecordCount")
            page_n = (start_index // _LIST_ITEMS_PAGE_SIZE) + 1
            if isinstance(total, int) and total > 0:
                total_pages = (total + _LIST_ITEMS_PAGE_SIZE - 1) // _LIST_ITEMS_PAGE_SIZE
                logger.info(
                    "{} /Items page {}/{} (library={}, StartIndex={}, items returned={}, library total={})",
                    self.vendor_name,
                    page_n,
                    total_pages,
                    library_id,
                    start_index,
                    len(raw_items),
                    total,
                )
            else:
                logger.info(
                    "{} /Items page {} (library={}, StartIndex={}, items returned={})",
                    self.vendor_name,
                    page_n,
                    library_id,
                    start_index,
                    len(raw_items),
                )
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                path = str(raw.get("Path") or "")
                if not path:
                    continue
                yield MediaItem(
                    id=str(raw.get("Id") or ""),
                    library_id=library_id,
                    title=_format_emby_title(raw),
                    remote_path=path,
                )

            if len(raw_items) < _LIST_ITEMS_PAGE_SIZE:
                return
            # Defence-in-depth: stop if the server told us the total and
            # we've already requested everything. Without this, a server
            # that returns a full page on the boundary would cost one
            # extra round-trip that confirms emptiness — cheap but noisy.
            if isinstance(total, int) and start_index + len(raw_items) >= total:
                return
            start_index += len(raw_items)

    def search_items(self, query: str, limit: int = 50) -> list[MediaItem]:
        """Two-pass search via the shared :class:`SearchQuery` abstraction.

        Pre-fix this called ``/Items?searchTerm=...`` directly with the
        raw query string. ``searchTerm`` is a substring matcher with no
        relevance ranking, so ``"the boys s01e01"`` returned every item
        containing the token "boys" (Wonder Boys, Nickel Boys, Jersey
        Boys, Bad Boys, Boys State, Good Boys) — the user got 6 wrong
        results before the right one. Live regression 2026-05-10.

        Two-pass strategy:

        1. **Series-first**: ``/Items?NameStartsWith=<title>&IncludeItemTypes=Series``
           — prefix-indexed lookup. When a Series matches we always
           follow up with ``/Shows/<series_id>/Episodes`` and either:

           * narrow to the requested ``S##E##`` if the query had one, or
           * emit every episode the show has (capped at ``limit``) so
             the inspector lets the user browse the whole show.

           Jellyfin requires a ``UserId`` query param on
           ``/Shows/{id}/Episodes`` for any non-public catalogue; we
           add it whenever auth captured one. Emby tolerates the
           extra param.

        2. **Fallback**: ``/Items?searchTerm=<title>`` against
           Series + Movie + Episode, then client-side rank with the
           shared :func:`rank_score` so a 1.0 exact-title-match
           ("The Boys") sorts above a 0.2 single-token match
           ("Wonder Boys"). Series-type rows are dropped
           unconditionally (Pass 1 already expanded them); when the
           query carries ``S##E##`` only matching episodes are kept.
           The 0.3 floor inside ``filter_and_rank`` drops the
           substring-only noise.

        Empty query → empty list (caller-handled). Both passes together
        return at most ``limit`` items.
        """
        from ..search import SearchQuery
        from ..search.rank import filter_and_rank

        sq = SearchQuery.parse(query)
        if sq.is_empty:
            return []

        results: list[MediaItem] = []
        seen_ids: set[str] = set()
        user_id = self._user_id()

        def _maybe_add_user_id(params: dict[str, Any]) -> dict[str, Any]:
            """Thread UserId into a /Items params dict when auth has one.

            Jellyfin's /Items endpoints return empty lists (or 401) without
            a UserId scope for any title that isn't in the public catalogue;
            Emby is permissive and accepts the param either way. Same
            vendor quirk documented at the /Items/{id} resolver below.
            """
            if user_id:
                return {**params, "UserId": user_id}
            return params

        def _expand_series(series_id: str) -> bool:
            """Drill into a Series via /Shows/{id}/Episodes and emit episodes.

            Used by both passes to turn show-folder Series rows into the
            actual media files the inspector can load. When ``sq.has_episode``
            only the matching ``(season, episode)`` is kept; otherwise every
            episode is emitted (capped at ``limit``).

            Returns ``True`` when the caller has filled the result list to
            ``limit`` and should stop. ``False`` to keep going.
            """
            ep_params: dict[str, Any] = {
                "Fields": "Path,IndexNumber,ParentIndexNumber",
                "Limit": "500",
            }
            if sq.has_episode:
                ep_params["Season"] = str(sq.season)
            ep_params = _maybe_add_user_id(ep_params)
            try:
                ep_response = self._request("GET", f"/Shows/{series_id}/Episodes", params=ep_params).json()
            except Exception as exc:
                logger.debug(
                    "[{}] Episodes lookup for series {} failed: {}",
                    self.name,
                    series_id,
                    exc,
                )
                return False
            for ep in ep_response.get("Items", []) or []:
                if not isinstance(ep, dict):
                    continue
                if sq.has_episode and ep.get("IndexNumber") != sq.episode:
                    continue
                path = str(ep.get("Path") or "")
                if not path:
                    continue
                eid = str(ep.get("Id") or "")
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                results.append(
                    MediaItem(
                        id=eid,
                        library_id=str(ep.get("ParentId") or series_id),
                        title=_format_emby_title(ep),
                        remote_path=path,
                    )
                )
                if len(results) >= limit:
                    return True
            return False

        # ---------------------------------------------------------------
        # Pass 1: Series-first via NameStartsWith.
        #
        # Fast prefix-indexed lookup on Emby. Jellyfin's NameStartsWith
        # has a documented quirk where titles starting with stop-words
        # like "The" don't match (verified against Jellyfin 10.11.8:
        # NameStartsWith="The Neighbourhood" returns 0, NameStartsWith=
        # "Neigh" returns the show). Pass 2's searchTerm fallback covers
        # that case via the same _expand_series helper.
        # ---------------------------------------------------------------
        series_params = _maybe_add_user_id(
            {
                "NameStartsWith": sq.title,
                "IncludeItemTypes": "Series",
                "Recursive": "true",
                "Fields": "Path",
                "Limit": "10",
            }
        )
        try:
            series_hits = self.query_items(series_params)
        except Exception as exc:
            logger.debug(
                "[{}] Series-first NameStartsWith pass failed for {!r}: {}",
                self.name,
                sq.raw,
                exc,
            )
            series_hits = []
        for series in series_hits or []:
            if not isinstance(series, dict):
                continue
            # NameStartsWith returns whatever matches the prefix; we asked
            # for Series only via IncludeItemTypes but a defensive check
            # protects against servers that ignore the filter (and tests
            # that share the same query_items mock for both passes).
            if str(series.get("Type") or "") != "Series":
                continue
            series_id = str(series.get("Id") or "")
            if not series_id:
                continue
            if _expand_series(series_id):
                return results

        # ---------------------------------------------------------------
        # Pass 2: searchTerm fallback with client-side rank.
        #
        # Covers movies, plain show-name queries on Jellyfin (where
        # Pass 1's NameStartsWith silently misses on "The"-prefixed
        # titles), and cases where the user typed a partial title
        # ("boys" for "The Boys").
        #
        # Series rows are NOT dropped — they're expanded into their
        # episodes via _expand_series so show-name queries always
        # surface loadable media files. Pre-fix this dropped Series
        # rows entirely, which left Jellyfin show searches with zero
        # results because Pass 1 had also missed.
        # ---------------------------------------------------------------
        fallback_params = _maybe_add_user_id(
            {
                "searchTerm": sq.title,
                "IncludeItemTypes": "Series,Movie,Episode",
                "Recursive": "true",
                # IndexNumber/ParentIndexNumber feed the has_episode filter
                # below; SeriesId is reserved for future show-grouping. Path
                # stays for the existing path-filter.
                "Fields": "Path,IndexNumber,ParentIndexNumber,SeriesId",
                # Emby/Jellyfin servers cap at varying limits; ask for plenty
                # so the rank pass has enough candidates to choose from
                # without blowing the wire.
                "Limit": str(min(int(limit) * 4, 200)),
            }
        )
        try:
            raw_items = self.query_items(fallback_params)
        except Exception as exc:
            logger.info(
                "[{}] searchTerm fallback failed for {!r}: {}. Returning what Series-first found.",
                self.name,
                sq.raw,
                exc,
            )
            raw_items = []

        candidates: list[tuple[str, str, dict]] = []
        for raw in raw_items or []:
            if not isinstance(raw, dict):
                continue
            ctype = str(raw.get("Type") or "").lower()
            cname = _format_emby_title(raw)
            candidates.append((cname, ctype, raw))

        ranked = filter_and_rank(sq, candidates, limit=limit * 2)
        for raw in ranked:
            if len(results) >= limit:
                break
            raw_type = str(raw.get("Type") or "")
            if raw_type == "Series":
                # Show-folder row → expand into episodes via the same
                # /Shows/{id}/Episodes helper Pass 1 uses. The seen_ids
                # set inside _expand_series prevents double-emit when
                # Pass 1 already covered this series.
                series_id = str(raw.get("Id") or "")
                if series_id and _expand_series(series_id):
                    break
                continue
            # When the user typed S##E##, only let through episodes that
            # match the requested season+episode. Pre-fix Pass 2 returned
            # every matching episode regardless of S##E##, so a
            # "S01E08" query for a show like "The Neighbourhood" surfaced
            # S01E01–S01E08 instead of just S01E08.
            if sq.has_episode:
                if raw_type != "Episode":
                    continue
                if raw.get("ParentIndexNumber") != sq.season:
                    continue
                if raw.get("IndexNumber") != sq.episode:
                    continue
            path = str(raw.get("Path") or "")
            if not path:
                continue
            rid = str(raw.get("Id") or "")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            results.append(
                MediaItem(
                    id=rid,
                    library_id=str(raw.get("ParentId") or ""),
                    title=_format_emby_title(raw),
                    remote_path=path,
                )
            )

        if not results:
            # Surface zero-result searches at INFO so users investigating
            # "why doesn't search work" can grep the logs without enabling
            # debug. Pre-fix this happened silently.
            logger.info(
                "[{}] Search returned no results for {!r} (parsed title={!r}, S{}E{})",
                self.name,
                sq.raw,
                sq.title,
                sq.season,
                sq.episode,
            )
        return results

    def resolve_item_to_remote_path(self, item_id: str) -> str | None:
        """Return ``MediaSources[0].Path`` (or top-level ``Path``) for ``item_id``.

        Three vendor-specific quirks need accommodating:

        * Emby's bare ``/Items/{id}`` returns 404 when no user context is
          attached → use the per-user endpoint
          ``/Users/{userId}/Items/{id}`` whenever a ``user_id`` was
          captured (password auth flow).
        * Jellyfin's bare ``/Items/{id}`` returns **400** under any auth
          shape (the route signature changed across versions).
        * The plural ``/Items?Ids={id}`` works for both vendors and both
          auth shapes; we use it as the universal fallback.

        Prefers ``MediaSources[0].Path`` over the top-level ``Path``
        because some item types only populate the media source.
        """
        user_id = self._user_id()
        if user_id:
            primary_path = f"/Users/{user_id}/Items/{item_id}"
            primary_params = {"Fields": "Path,MediaSources"}
            primary_unwrap = lambda data: data  # noqa: E731 — single-item endpoint returns the item directly
        else:
            primary_path = "/Items"
            primary_params = {"Ids": item_id, "Fields": "Path,MediaSources"}

            def primary_unwrap(data):
                items = data.get("Items") or []
                return items[0] if items else {}

        try:
            response = self._request("GET", primary_path, params=primary_params)
            response.raise_for_status()
            data = primary_unwrap(response.json())
        except Exception as exc:
            logger.debug("{} item lookup failed for {}: {}", self.vendor_name, item_id, exc)
            return None

        if not isinstance(data, dict):
            return None

        for source in data.get("MediaSources", []) or []:
            if isinstance(source, dict):
                path = str(source.get("Path") or "")
                if path:
                    return path

        path = str(data.get("Path") or "")
        return path or None

    def _resolve_one_path(self, server_view_path: str) -> str | None:
        """Cached per-server-view-path lookup.

        The base class loops mapped candidates through this hook (see
        :meth:`MediaServer.resolve_remote_path_to_item_id`). This
        wrapper TTL-caches **positive** results — the dominant cost
        is a Pass-2 enumeration (~30s cold on a 200K-item Jellyfin)
        and the same server is typically asked about 200+ files in a
        row during a full-library scan.

        **Negative** results are NOT cached. The retry queue
        (``processing/retry_queue.py``) captures the registry — and
        therefore this server instance — in a closure when it arms a
        chain on ``PUBLISHED_PENDING_REGISTRATION``. The chain's
        first two backoff intervals (60s + 120s) both sit inside the
        300s positive-TTL window, so a cached ``None`` from the
        originating dispatch would force every early retry to
        short-circuit without re-querying — even after the server had
        finished indexing the file seconds later. Live regression
        (chain ``62e32c35``, Jonestown movie, 2026-05-11 22:30→22:38):
        Jellyfin finished scanning within ~60s of the chain starting,
        but the cached ``None`` lasted the full 300s TTL and wasted
        attempts #1 and #2 (both returned in 0.0s instead of
        re-hitting the Bridge plugin). The chain only recovered at
        attempt #3 (T+480s) when the TTL expired.

        Best-effort match: ``_uncached_resolve_remote_path_to_item_id``
        does a basename + trailing-two-component path-tail check
        against each candidate the underlying API returns. Subclasses
        replace just the uncached body with their vendor-native
        per-path lookup (Emby's ``Path=<exact>`` filter, Jellyfin's
        ``MediaPreviewBridge/ResolvePath``).
        """
        basename = os.path.basename(server_view_path or "")
        if not basename:
            return None

        now = time.monotonic()
        with self._reverse_lookup_lock:
            cached = self._reverse_lookup_cache.get(server_view_path)
            # ``cached[1] is not None`` guards against stale negatives
            # left in the cache by older code paths (defence-in-depth
            # for rolling deploys); current code never writes them.
            if cached is not None and cached[0] > now and cached[1] is not None:
                return cached[1]
        result = self._uncached_resolve_remote_path_to_item_id(server_view_path)
        if result is not None:
            with self._reverse_lookup_lock:
                self._reverse_lookup_cache[server_view_path] = (now + _REVERSE_LOOKUP_TTL_S, result)
        return result

    def _find_owning_library_id(self, remote_path: str) -> str | None:
        """Return the ParentId of the library whose location prefix contains ``remote_path``.

        Used to scope the reverse-lookup search to a single library
        (1 sec on Jellyfin) instead of enumerating the whole server
        (~30 s when Pass 1 misses on a 200K-item index). Also acts as
        a short-circuit: when ``self._config.libraries`` is populated
        AND no location matches, the file definitively isn't in any
        library on this server, so we can skip the network call
        entirely and return ``None`` in microseconds.

        Returns ``None`` when the cache is empty (libraries not yet
        loaded) so the caller falls through to the legacy unscoped
        search instead of incorrectly short-circuiting on a cold start.
        """
        libs = self._config.libraries or []
        if not libs:
            return None
        if not remote_path:
            return None
        rp = remote_path.replace("\\", "/").rstrip("/")
        for lib in libs:
            for loc in lib.remote_paths or ():
                loc_norm = str(loc).replace("\\", "/").rstrip("/")
                if loc_norm and (rp == loc_norm or rp.startswith(loc_norm + "/")):
                    return lib.id
        return None

    def _uncached_resolve_remote_path_to_item_id(self, remote_path: str) -> str | None:
        """Bypass-cache path-to-item-id lookup. See ``resolve_remote_path_to_item_id``."""
        basename = os.path.basename(remote_path or "")
        if not basename:
            return None

        stem = os.path.splitext(basename)[0]
        target_tail = "/".join(remote_path.rstrip("/").split("/")[-2:])

        # Library scoping — short-circuits when this server has libraries
        # cached but none of them contain a location prefix matching
        # ``remote_path``. Pre-rebuild this took 28-30 s per file on a
        # 200K-item Jellyfin/Emby; with scoping, the same lookup is
        # ~150 ms when the file isn't in this server's index. When the
        # cache is empty (cold start) parent_id is None and the queries
        # fall back to legacy unscoped behavior.
        parent_id = self._find_owning_library_id(remote_path)
        if parent_id is None and (self._config.libraries or []):
            # Cache populated, no match → definitively not in this server.
            logger.debug(
                "{} reverse-lookup short-circuit for {!r}: no library location prefix matches; skipping API call.",
                self.vendor_name,
                remote_path,
            )
            return None

        def _match(items: list) -> str | None:
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                path = str(raw.get("Path") or "")
                if not path:
                    continue
                if os.path.basename(path) == basename and path.replace("\\", "/").endswith(target_tail):
                    item_id = str(raw.get("Id") or "")
                    if item_id:
                        return item_id
            return None

        # Pass 0 — Name-prefix scoped lookup. The full filename stem the
        # legacy Pass 1 sends to ``searchTerm`` (e.g. "Show (2025) - S01E03 -
        # Title [WEBDL-1080p][EAC3 5.1][h264]-GROUP") tokenises into ~10
        # terms (year, codec tags, brackets, release group) and triggers
        # Emby's full-text scoring loop across every Movie/Episode in scope —
        # 30-76 s and 100% CPU on a 117K-episode library. The fast path
        # extracts just the show/movie title from the path components,
        # uses ``NameStartsWith`` (B-tree on the indexed Name column,
        # ~10 ms), then enumerates only the matching Series/Movies for a
        # local basename check. Empirical: ~10-20 ms per call vs 30-76 s,
        # with 100% Id agreement against Pass 1+2 across 22 real paths
        # (see tools/bench_emby_lookup.py if re-running).
        #
        # Two miss flavours, distinguished by ``definitive_miss``:
        # * ``definitive_miss=True`` — prefix extracted cleanly AND
        #   ``NameStartsWith`` (scoped by the known ParentId) returned
        #   zero candidates. No item in this library starts with that
        #   prefix; the file isn't here. Short-circuit Pass 1+2 — the
        #   30s scoring loop is the dominant cost of multi-server
        #   webhook setups where one server simply doesn't own the
        #   path. (perf #44)
        # * ``definitive_miss=False`` — every other miss case: prefix
        #   extraction failed, cap busted, network/parse error, or
        #   candidates found but local basename match missed. Fall
        #   through to Pass 1+2 to preserve recall on edge cases
        #   (folder name fundamentally differs from Emby's stored
        #   Name, basename slightly varied, etc.).
        #
        # Jellyfin note: ``JellyfinServer`` extends this base class, so
        # the short-circuit applies to it too. Jellyfin's ``Items?Path=``
        # is indexed and cheap (no 30s scoring pathology), so the perf
        # payoff is much smaller there — but the recall trade-off is
        # the same, and it's strictly an improvement (skipping a fast
        # query is still faster than running it). For users with the
        # MediaPreviewBridge plugin installed, ``JellyfinServer._resolve_one_path``
        # short-circuits earlier on the plugin's authoritative answer
        # and never reaches this base implementation at all.
        if parent_id:
            pass0_id, definitive_miss = self._pass0_name_prefix_lookup(remote_path, basename, target_tail, parent_id)
            if pass0_id is not None:
                return pass0_id
            if definitive_miss:
                logger.debug(
                    "{} reverse-lookup short-circuit for {!r}: Pass 0 NameStartsWith "
                    "returned 0 candidates with prefix scoped to ParentId={}; skipping "
                    "Pass 1+2 (perf #44).",
                    self.vendor_name,
                    remote_path,
                    parent_id,
                )
                return None

        # Pass 1 — cheap searchTerm query, scoped to the owning library
        # when we have its id.
        pass1_params = {
            "searchTerm": stem,
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode",
            "Fields": "Path",
            "Limit": 50,
        }
        if parent_id:
            pass1_params["ParentId"] = parent_id
        try:
            response = self._request("GET", "/Items", params=pass1_params)
            response.raise_for_status()
            hit = _match(response.json().get("Items") or [])
            if hit:
                return hit
        except Exception as exc:
            logger.debug("{} reverse-lookup search failed for {}: {}", self.vendor_name, remote_path, exc)

        # Pass 2 — enumeration fallback for titles whose tokens (4K, HDR,
        # DV, etc.) the search index quietly drops. Scoped to the owning
        # library when we have its id, so a "not found" decision lands
        # in ~1 sec instead of a full-server walk.
        pass2_params = {
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode",
            "Fields": "Path",
            "Limit": 1000,
        }
        if parent_id:
            pass2_params["ParentId"] = parent_id
        try:
            response = self._request("GET", "/Items", params=pass2_params)
            response.raise_for_status()
            return _match(response.json().get("Items") or [])
        except Exception as exc:
            logger.debug("{} reverse-lookup enumerate failed for {}: {}", self.vendor_name, remote_path, exc)
            return None

    # NameStartsWith fast path — see Pass 0 comment in
    # ``_uncached_resolve_remote_path_to_item_id`` above.

    # Cap on Series/Movie candidates returned by NameStartsWith. We use
    # only the FIRST WORD of the cleaned title as the prefix; common
    # words ("Black"=58, "One"=53, "American"=95, "Be"=256) return
    # large candidate sets on a big library. Walking 500 series +
    # their episode lists is ~2.5 s vs Pass 1's 30-76 s; well worth
    # the extra round-trips to keep recall above 95% on the live
    # miss-audit. Above the cap the fast path aborts and we fall
    # through to Pass 1+2.
    _PASS0_PARENT_CANDIDATE_CAP = 500
    # Per-series episode enumerate Limit. Long-running shows have
    # huge episode counts (Pokémon 1266, Doctor Who 870+, Simpsons
    # 800+) and a low cap silently truncates the result so the local
    # basename match misses the target episode. 2000 covers every
    # practical case; the JSON payload is tolerable (~1 MB on the
    # extreme) since it's local network and only fires on a Pass-0
    # match.
    _PASS0_EPISODE_LIMIT = 2000

    # ``(YYYY)`` and ``[bracket-tag]`` patterns the show/movie folder
    # accumulates (year, imdb id, release group). Strip both from the
    # candidate before sending to NameStartsWith — Emby's stored Name
    # is the bare title.
    _PASS0_YEAR_RE = re.compile(r"\s*\([0-9]{4}\)\s*")
    _PASS0_BRACKET_RE = re.compile(r"\s*\[[^]]+\]\s*")
    # Emby/Jellyfin's NameStartsWith filter matches against ``SortName``,
    # not ``Name``. SortName strips leading English articles, so
    # "The Matrix" sorts under "Matrix" and "The 'Burbs" under "'Burbs".
    # Empirically confirmed on a real Emby instance: item Name="The 'Burbs"
    # has SortName="'Burbs"; NameStartsWith="The 'Burbs" returns 0,
    # NameStartsWith="'Burbs" returns 1.
    _PASS0_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
    # SortName ALSO normalises Unicode accents away — Name="Pokémon"
    # has SortName="Pokemon". Without normalisation the prefix
    # "Pokémon" returns 0 results from NameStartsWith. Strip combining
    # marks (NFD decompose + filter category Mn) so the prefix matches.
    # Empirically confirmed: NameStartsWith="Pokémon"→0,
    # NameStartsWith="Pokemon"→2 ("Pokémon", "Pokémon Concierge").
    # Episode pattern in basename — ``S01E03``, ``s1e3``, ``1x03`` etc.
    # Used to override the Season-folder heuristic for flat TV layouts
    # (``/Show/episode.mkv`` instead of ``/Show/Season 01/...``). Without
    # this, flat-layout episodes would query for Movie types and miss.
    _PASS0_EPISODE_PATTERN_RE = re.compile(r"(?:S\d+E\d+|s\d+e\d+|\d+x\d+)", re.IGNORECASE)

    def _extract_title_prefix(self, remote_path: str) -> tuple[str, bool] | None:
        """Extract a NameStartsWith candidate + episode/movie kind hint.

        Returns ``(prefix, is_episode)`` where ``prefix`` is the FIRST
        WORD of the cleaned show/movie title (lowercased article
        stripped, accents normalised, year + brackets dropped) and
        ``is_episode`` indicates whether the path resolves to a TV
        episode.

        Why first word only — empirically validated against a 100-item
        random sample of EmbyTest:
        * Path-derived names often differ from Emby's stored Name on
          internal characters: ``"TRON Legacy"`` (path) vs ``"TRON: Legacy"``
          (Emby), ``"Baki-Dou - The Invincible Samurai"`` vs
          ``"BAKI-DOU: The Invincible Samurai"``,
          ``"Larry The Cable Guy Remain Seated"`` vs
          ``"Larry the Cable Guy: Remain Seated"``. NameStartsWith on the
          full path-derived string misses every one of those (~17%
          miss rate). The first word is the most reliable shared prefix.
        * NameStartsWith is case-insensitive on Emby/Jellyfin so
          ``"BAKI"`` ↔ ``"Baki"`` work either way.
        * Local match by basename + path tail still narrows the
          candidate set to the exact target file, so a broader prefix
          doesn't cause false positives — only extra round-trips, capped
          at ``_PASS0_PARENT_CANDIDATE_CAP`` series.

        Returns ``None`` when no usable first word can be derived, in
        which case the caller falls back to Pass 1+2.
        """
        if not remote_path:
            return None
        parts = remote_path.replace("\\", "/").split("/")
        if not parts:
            return None
        candidate = None
        is_episode = False
        for i, comp in enumerate(parts):
            # Match "Season 01", "Season 1", "season1" etc.
            if re.match(r"^season\b", comp, re.IGNORECASE) and i > 0:
                candidate = parts[i - 1]
                is_episode = True
                break
        if candidate is None and len(parts) >= 2:
            # Movie layout or flat-TV layout — parent dir of the file.
            candidate = parts[-2]
        if not candidate:
            return None
        # Flat-TV layout fallback: a path like
        #   /TV/Bewitched (1964)/Bewitched (1964) - S05E17 - Title.mkv
        # has no Season folder, so the loop above didn't set is_episode,
        # but the basename clearly carries an episode token. Promote.
        basename = parts[-1]
        if not is_episode and self._PASS0_EPISODE_PATTERN_RE.search(basename):
            is_episode = True

        cleaned = self._PASS0_YEAR_RE.sub(" ", candidate)
        cleaned = self._PASS0_BRACKET_RE.sub(" ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        cleaned = self._PASS0_LEADING_ARTICLE_RE.sub("", cleaned, count=1).strip()
        # SortName drops accents (Pokémon → Pokemon). NFD decomposes
        # base+combining-mark, then drop everything in category Mn.
        cleaned = "".join(c for c in unicodedata.normalize("NFD", cleaned) if unicodedata.category(c) != "Mn")
        cleaned = cleaned.strip()
        if not cleaned:
            return None
        # First word only — see method docstring for rationale.
        first = cleaned.split(maxsplit=1)[0]
        if len(first) < 2:
            return None
        return first, is_episode

    def _pass0_name_prefix_lookup(
        self,
        remote_path: str,
        basename: str,
        target_tail: str,
        parent_id: str,
    ) -> tuple[str | None, bool]:
        """Try NameStartsWith → per-Series enumerate before slow Pass 1+2.

        Returns:
            ``(item_id, definitive_miss)``:

            * ``(str, _)`` — found; ``item_id`` is the resolved Emby Id.
            * ``(None, True)`` — Pass 0 conclusively confirmed the file
              isn't in this library. Specifically: prefix extracted
              cleanly AND ``NameStartsWith`` (scoped by ``ParentId``)
              returned zero candidates. Caller skips Pass 1+2 (perf #44).
            * ``(None, False)`` — Pass 0 was indeterminate. Caller falls
              through to Pass 1+2 for recall safety. Includes:
              prefix-extraction failure, cap-busted candidate set,
              network/parse error, candidates found but local basename
              match missed.
        """
        extracted = self._extract_title_prefix(remote_path)
        if not extracted:
            return (None, False)
        title_prefix, is_episode = extracted

        # Step 1: NameStartsWith → small candidate set scoped to library.
        # Path-derived hint (Season folder present?) is more reliable than
        # the Library kind cache (which can be stale on a fresh start).
        include_types = "Series" if is_episode else "Movie"
        step1_params = {
            "Recursive": "true",
            "IncludeItemTypes": include_types,
            "NameStartsWith": title_prefix,
            "Fields": "Path",
            "Limit": self._PASS0_PARENT_CANDIDATE_CAP,
            "ParentId": parent_id,
        }
        try:
            response = self._request("GET", "/Items", params=step1_params)
            response.raise_for_status()
            body = response.json() or {}
            candidates = body.get("Items") or []
            total = body.get("TotalRecordCount")
        except Exception as exc:
            logger.debug(
                "{} pass-0 NameStartsWith failed for {!r}: {} — falling back",
                self.vendor_name,
                remote_path,
                exc,
            )
            return (None, False)

        if not candidates:
            # ParentId-scoped query with a cleanly-extracted prefix
            # returned zero. No show/movie in this library starts with
            # that prefix → the file genuinely isn't indexed here.
            # Definitive negative — caller can skip Pass 1+2.
            return (None, True)
        # Cap busted: a too-broad prefix returned more than we'd want to
        # walk. Abort so Pass 1's scoring narrows the field instead.
        if isinstance(total, int) and total > self._PASS0_PARENT_CANDIDATE_CAP:
            logger.debug(
                "{} pass-0 NameStartsWith for prefix {!r} returned {} candidates (>{}); falling back to searchTerm.",
                self.vendor_name,
                title_prefix,
                total,
                self._PASS0_PARENT_CANDIDATE_CAP,
            )
            return (None, False)

        # Step 2: movies match directly on the candidate set; episodes
        # need a per-Series enumerate (each Series has 10-300 episodes,
        # tiny vs the 1000-cap library walk Pass 2 does).
        #
        # Definitive-miss reasoning for the "candidates returned but no
        # basename match" case (live perf #44 follow-up): the dominant
        # multi-server scenario is "show/movie isn't on this server but
        # other items share the first word" — e.g. Boy Band Confidential
        # webhook fires, EmbyTest has 23 other ``Boy*`` shows. Without
        # this branch, Pass 0 walked every sibling's episodes (correct,
        # ~1s on TV libraries), found no match, then fell through to
        # Pass 1's 30s scoring loop just to confirm what we already knew.
        # Short-circuit to skip Pass 1+2.
        #
        # Recall trade: an item that IS in Emby/Jellyfin but stored
        # under a series Name whose first word fundamentally differs
        # from the folder name (~1% rate per the live audit in commit
        # f211dd7) misses on this webhook. Recovers via the next
        # webhook fire, scheduled scan, or manual refresh.
        if not is_episode:
            hit = self._match_basename(candidates, basename, target_tail)
            # Movies: the candidate set already contained Path on every
            # entry, so we did exhaustive matching. ``hit is None`` →
            # definitive miss.
            return (hit, hit is None)

        # Episodes: walk each candidate Series's episodes. Track whether
        # any per-series enumerate ERRORED — if so, we can't be sure
        # the file isn't in a series we couldn't read, so the miss is
        # indeterminate (preserve recall via Pass 1+2).
        any_enumerate_error = False
        for series in candidates:
            if not isinstance(series, dict):
                continue
            series_id = str(series.get("Id") or "")
            if not series_id:
                continue
            ep_params = {
                "Recursive": "true",
                "IncludeItemTypes": "Episode",
                "Fields": "Path",
                "Limit": self._PASS0_EPISODE_LIMIT,
                "ParentId": series_id,
            }
            try:
                ep_response = self._request("GET", "/Items", params=ep_params)
                ep_response.raise_for_status()
                episodes = ep_response.json().get("Items") or []
            except Exception as exc:
                logger.debug(
                    "{} pass-0 episode enumerate for series {} failed: {} — trying next candidate",
                    self.vendor_name,
                    series_id,
                    exc,
                )
                any_enumerate_error = True
                continue
            hit = self._match_basename(episodes, basename, target_tail)
            if hit:
                return (hit, False)
        # All candidate Series enumerated successfully and none contained
        # the target episode → definitive miss. Any error → indeterminate
        # (the missed-enumerate series might have had the file).
        return (None, not any_enumerate_error)

    @staticmethod
    def _match_basename(items: list, basename: str, target_tail: str) -> str | None:
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            path = str(raw.get("Path") or "")
            if not path:
                continue
            if os.path.basename(path) == basename and path.replace("\\", "/").endswith(target_tail):
                item_id = str(raw.get("Id") or "")
                if item_id:
                    return item_id
        return None


def _format_emby_title(item: dict[str, Any]) -> str:
    """Build a human-readable title for an Emby/Jellyfin item.

    For episodes, returns ``"<Series> S01E02"``; for movies and other
    item types, returns the raw ``Name``. The two vendors share this
    convention (Jellyfin forked from Emby), so a single helper covers
    both.
    """
    item_type = str(item.get("Type") or "")
    name = str(item.get("Name") or "")
    if item_type == "Episode":
        series = str(item.get("SeriesName") or "")
        season = item.get("ParentIndexNumber")
        episode = item.get("IndexNumber")
        if series and season is not None and episode is not None:
            return f"{series} S{int(season):02d}E{int(episode):02d}"
    return name
