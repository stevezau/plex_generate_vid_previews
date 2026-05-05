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
        # Reverse-lookup cache: ``{remote_path: (expires_at, item_id_or_none)}``.
        # Caches BOTH positive ("found item X") and negative ("not in library")
        # results — the negative case is the dominant cost (Pass-2 enum is
        # ~30s cold), and getting a stale negative is harmless because the
        # SKIPPED_NOT_IN_LIBRARY retry queue picks the file up later anyway.
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
    ) -> requests.Response:
        """Issue an authenticated request against the server's HTTP API."""
        url = f"{self._config.url.rstrip('/')}{path}"
        headers = {
            "X-Emby-Token": self._token(),
            "Accept": "application/json",
        }
        verify = bool(self._config.verify_ssl)
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return self._get_session().request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=int(self._config.timeout or 30),
            verify=verify,
        )

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
        """Yield every video :class:`MediaItem` inside the given library."""
        params = {
            "ParentId": library_id,
            "IncludeItemTypes": "Movie,Episode",
            "Recursive": "true",
            "Fields": "Path",
            "Limit": 5000,
        }
        try:
            response = self._request("GET", "/Items", params=params)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning(
                "Could not list items in {} library {} ({}: {}). "
                "This library will be skipped for this run — verify the server is reachable, the API key / token "
                "is still valid, and that the library hasn't been deleted on the server side.",
                self.vendor_name,
                library_id,
                type(exc).__name__,
                exc,
            )
            return

        for raw in payload.get("Items", []) or []:
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

    def search_items(self, query: str, limit: int = 50) -> list[MediaItem]:
        """Search via ``/Items?searchTerm=…&Recursive=true`` (server-side index).

        Both Emby and Jellyfin expose ``searchTerm`` on the ``/Items``
        endpoint; the server filters and returns only matches in one
        round-trip. The base-class default would walk every library and
        every item — D4 fix on Plex side; same brute-force walk hits
        Emby/Jellyfin equally hard on big installs.
        """
        needle = (query or "").strip()
        if not needle:
            return []
        params = {
            "searchTerm": needle,
            "IncludeItemTypes": "Movie,Episode",
            "Recursive": "true",
            "Fields": "Path",
            "Limit": str(int(limit)),
        }
        raw_items = self.query_items(params)
        results: list[MediaItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            path = str(raw.get("Path") or "")
            if not path:
                # Series/season-level matches don't carry a file path; skip
                # so the inspector list never shows un-clickable rows.
                continue
            results.append(
                MediaItem(
                    id=str(raw.get("Id") or ""),
                    library_id=str(raw.get("ParentId") or ""),
                    title=_format_emby_title(raw),
                    remote_path=path,
                )
            )
            if len(results) >= limit:
                break
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
        wrapper TTL-caches results — both positive AND negative —
        because the dominant cost is a Pass-2 enumeration (~30s cold
        on a 200K-item Jellyfin) and the same server is typically
        asked about 200+ files in a row during a full-library scan.

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

        # Cache check first.
        now = time.monotonic()
        with self._reverse_lookup_lock:
            cached = self._reverse_lookup_cache.get(server_view_path)
            if cached is not None and cached[0] > now:
                return cached[1]
        result = self._uncached_resolve_remote_path_to_item_id(server_view_path)
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
