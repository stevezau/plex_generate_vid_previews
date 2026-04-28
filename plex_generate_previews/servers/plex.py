"""Plex implementation of the :class:`MediaServer` interface.

This is a thin façade over the existing :mod:`plex_generate_previews.plex_client`
helpers so the rest of the codebase can be migrated to the abstract interface
without rewriting Plex-specific logic. As the multi-server refactor lands, the
inline calls in :mod:`processing.orchestrator` and :mod:`web.webhooks` are
re-routed through this class.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import requests
import urllib3
from loguru import logger

from .base import (
    ConnectionResult,
    Library,
    MediaItem,
    MediaServer,
    ServerType,
    WebhookEvent,
)

if TYPE_CHECKING:
    from ..config import Config


class PlexServer(MediaServer):
    """Wrap a single Plex Media Server in the :class:`MediaServer` interface.

    Construction takes the legacy :class:`Config` directly so existing helpers
    (which all key off ``config.plex_*`` fields) keep working unchanged. A
    later refactor switches this to take a per-server ``ServerConfig``.

    The underlying ``plexapi`` connection is created lazily on first use; the
    class is therefore cheap to instantiate from configuration without paying
    a round-trip cost.
    """

    def __init__(
        self,
        config: Config,
        *,
        server_id: str = "plex",
        name: str = "Plex",
    ) -> None:
        super().__init__(server_id=server_id, name=name)
        self._config = config
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
        """Enumerate libraries from Plex, applying the user's enabled-list filter."""
        from ..plex_client import retry_plex_call

        try:
            sections = retry_plex_call(self._connect().library.sections)
        except Exception as exc:
            logger.warning("Failed to enumerate Plex library sections: {}", exc)
            return []

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
            if selected_ids:
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
            logger.warning("Failed to enumerate Plex sections for list_items: {}", exc)
            return

        target = next(
            (s for s in sections if str(getattr(s, "key", "") or "") == str(library_id)),
            None,
        )
        if target is None:
            logger.warning("Plex library id {} not found", library_id)
            return

        try:
            if target.METADATA_TYPE == "episode":
                results = retry_plex_call(target.search, libtype="episode")
                for m in results:
                    locations = _extract_item_locations(m)
                    if not locations:
                        continue
                    yield MediaItem(
                        id=str(m.key),
                        library_id=str(target.key),
                        title=_build_episode_title(m),
                        remote_path=str(locations[0]),
                    )
            elif target.METADATA_TYPE == "movie":
                for m in retry_plex_call(target.search):
                    locations = _extract_item_locations(m)
                    if not locations:
                        continue
                    yield MediaItem(
                        id=str(m.key),
                        library_id=str(target.key),
                        title=str(getattr(m, "title", "") or ""),
                        remote_path=str(locations[0]),
                    )
            else:
                logger.info(
                    "Skipping Plex library {} (unsupported METADATA_TYPE={})",
                    target.title,
                    target.METADATA_TYPE,
                )
        except Exception as exc:
            logger.warning("Failed to enumerate items in Plex library {}: {}", target.title, exc)

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

    def resolve_remote_path_to_item_id(self, remote_path: str) -> str | None:
        """Return the Plex ratingKey for the file at ``remote_path``.

        Walks every enabled library section and searches for an item
        whose first MediaPart's ``file`` ends with the basename and
        trailing parent dir of ``remote_path``. Used by the dispatcher
        when a path-based webhook (Sonarr/Radarr) fires and the
        :class:`PlexBundleAdapter` needs an item id to look up the
        bundle hash.

        Returns ``None`` when no match exists or the API call fails;
        the dispatcher then routes the publisher to the slow-backoff
        retry queue (the file may not yet be indexed).
        """
        import os as _os

        from ..plex_client import retry_plex_call

        if not remote_path:
            return None

        basename = _os.path.basename(remote_path)
        if not basename:
            return None
        target_tail = "/".join(remote_path.rstrip("/").split("/")[-2:])

        try:
            plex = self._connect()
            sections = retry_plex_call(plex.library.sections)
        except Exception as exc:
            logger.debug("Plex reverse-lookup: section enumeration failed: {}", exc)
            return None

        # Search by basename within each section. plexapi's search is
        # by title/keyword, not filename, so we iterate all() and
        # match on the part path. Scales with library size, but for
        # the dispatcher's use this fires once per webhook, and
        # ratingKey caching upstream means the cost is bounded.
        for section in sections:
            try:
                items = retry_plex_call(section.all)
            except Exception as exc:
                logger.debug("Plex reverse-lookup: section.all() failed for {}: {}", section, exc)
                continue
            for item in items:
                for media in getattr(item, "media", None) or []:
                    for part in getattr(media, "parts", None) or []:
                        file_path = str(getattr(part, "file", None) or "")
                        if not file_path:
                            continue
                        if _os.path.basename(file_path) == basename and file_path.replace("\\", "/").endswith(
                            target_tail
                        ):
                            rating_key = getattr(item, "ratingKey", None)
                            if rating_key is not None:
                                return str(rating_key)
        return None

    def trigger_refresh(self, *, item_id: str | None, remote_path: str | None) -> None:
        """Trigger a partial Plex library scan for ``remote_path``.

        Plex's targeted-scan endpoint accepts a folder path within a library
        section, so we delegate to the existing
        :func:`plex_client.trigger_plex_partial_scan` helper. Item-id-only
        refresh is not natively supported; callers should pass a path when
        they have one.
        """
        if not remote_path:
            return
        from ..plex_client import trigger_plex_partial_scan

        path_mappings = getattr(self._config, "path_mappings", None) or []
        try:
            trigger_plex_partial_scan(
                plex_url=self._config.plex_url,
                plex_token=self._config.plex_token,
                unresolved_paths=[remote_path],
                path_mappings=path_mappings,
                verify_ssl=bool(getattr(self._config, "plex_verify_ssl", True)),
            )
        except Exception as exc:
            logger.debug("Plex partial scan trigger failed for {}: {}", remote_path, exc)

    def get_bundle_metadata(self, item_id: str) -> list[tuple[str, str]]:
        """Return ``(bundle_hash, remote_path)`` for every MediaPart of an item.

        Plex-specific helper (not part of the abstract :class:`MediaServer`
        interface) used by :class:`PlexBundleAdapter` to compute the BIF output
        location. Plex's ``/library/metadata/{id}/tree`` endpoint returns XML;
        we surface the relevant attributes as plain tuples.

        Returns an empty list when the lookup fails or the item has no parts —
        the adapter translates that into a
        :class:`~plex_generate_previews.servers.LibraryNotYetIndexedError`.
        """
        from ..plex_client import retry_plex_call

        try:
            data = retry_plex_call(self._connect().query, f"/library/metadata/{item_id}/tree")
        except Exception as exc:
            logger.debug("Plex /tree query failed for {}: {}", item_id, exc)
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
