"""Tests for the universal webhook router (auto-detection + dispatch)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from media_preview_generator.processing.multi_server import (
    MultiServerResult,
    MultiServerStatus,
    PublisherResult,
    PublisherStatus,
)
from media_preview_generator.web.settings_manager import get_settings_manager


@pytest.fixture
def mock_auth_config(tmp_path, monkeypatch):
    auth_file = str(tmp_path / "auth.json")
    monkeypatch.setattr("media_preview_generator.web.auth.AUTH_FILE", auth_file)
    monkeypatch.setattr("media_preview_generator.web.auth.get_config_dir", lambda: str(tmp_path))
    from media_preview_generator.web.settings_manager import reset_settings_manager

    reset_settings_manager()
    from media_preview_generator.web.routes import clear_gpu_cache

    clear_gpu_cache()
    return str(tmp_path)


@pytest.fixture
def flask_app(tmp_path, mock_auth_config):
    from media_preview_generator.web.app import create_app

    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture
def webhook_token():
    """Configure a known webhook secret and return it.

    The existing webhooks_bp authenticator reads ``webhook_secret``
    (not ``webhook_token``) from settings — match that key name.
    """
    sm = get_settings_manager()
    sm.set("webhook_secret", "test-token")
    return "test-token"


@pytest.fixture
def auth_headers(webhook_token):
    return {"X-Auth-Token": webhook_token}


def _published_result() -> MultiServerResult:
    return MultiServerResult(
        canonical_path="/data/movies/Foo.mkv",
        status=MultiServerStatus.PUBLISHED,
        publishers=[
            PublisherResult(
                server_id="emby-1",
                server_name="Emby",
                adapter_name="emby_sidecar",
                status=PublisherStatus.PUBLISHED,
                message="Published",
            )
        ],
        frame_count=10,
        message="1 of 1 publisher(s) succeeded",
    )


def _seed_servers(servers: list[dict]) -> None:
    sm = get_settings_manager()
    sm.set("media_servers", servers)


class TestSonarrWebhook:
    def test_path_payload_dispatched(self, client, auth_headers):
        _seed_servers(
            [
                {
                    "id": "emby-1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby:8096",
                    "auth": {},
                    "libraries": [{"id": "1", "name": "TV", "remote_paths": ["/data/tv"], "enabled": True}],
                }
            ]
        )

        with patch(
            "media_preview_generator.web.webhook_router.create_vendor_webhook_job",
            return_value="job-fake-12345678",
        ) as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "eventType": "Download",
                        "series": {"path": "/data/tv/Foo"},
                        "episodeFile": {"path": "/data/tv/Foo/S01E01.mkv"},
                    }
                ),
            )

        assert response.status_code == 202
        body = response.get_json()
        assert body["status"] == "queued"
        assert body["kind"] == "sonarr"
        proc.assert_called_once()
        assert proc.call_args.kwargs["canonical_path"] == "/data/tv/Foo/S01E01.mkv"
        assert proc.call_args.kwargs["source"] == "sonarr", (
            "source label flows into the Job UI source chip — silent re-tagging would mislead operators"
        )

    def test_radarr_payload_classified_correctly(self, client, auth_headers):
        # No pre-flight check anymore — the router creates a Job for every
        # webhook (even ones the orchestrator may later determine have no
        # owners). The seed is just defensive in case the test is later
        # extended to assert against the real dispatch path.
        with patch(
            "media_preview_generator.web.webhook_router.create_vendor_webhook_job",
            return_value="job-fake-12345678",
        ) as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "eventType": "Download",
                        "movie": {"folderPath": "/data/movies/Bar"},
                        "movieFile": {"path": "/data/movies/Bar/Bar.mkv"},
                    }
                ),
            )

        assert response.status_code == 202
        assert response.get_json()["kind"] == "radarr"
        proc.assert_called_once()
        # Audit fix — also assert the canonical_path the route resolved
        # from the Radarr payload (folderPath + relativePath join). Without
        # this, a regression that called create_vendor_webhook_job with
        # the wrong path would pass (D34 pattern).
        assert proc.call_args.kwargs["canonical_path"] == "/data/movies/Bar/Bar.mkv"
        assert proc.call_args.kwargs["source"] == "radarr"


class TestJellyfinWebhook:
    def test_itemadded_with_servers_id_match(self, client, auth_headers, monkeypatch):
        _seed_servers(
            [
                {
                    "id": "jelly-1",
                    "type": "jellyfin",
                    "name": "Jellyfin",
                    "enabled": True,
                    "url": "http://jellyfin:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "libraries": [{"id": "1", "name": "TV", "remote_paths": ["/data/tv"], "enabled": True}],
                }
            ]
        )

        # Stub Jellyfin's path resolution.
        from media_preview_generator.servers.jellyfin import JellyfinServer

        monkeypatch.setattr(
            JellyfinServer,
            "resolve_item_to_remote_path",
            lambda self, item_id: "/data/tv/Show/S01E01.mkv",
        )

        with patch(
            "media_preview_generator.web.webhook_router.create_vendor_webhook_job",
            return_value="job-fake-12345678",
        ) as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "NotificationType": "ItemAdded",
                        "ItemId": "jf-42",
                        "ItemType": "Episode",
                        "ServerId": "jelly-1",
                    }
                ),
            )

        assert response.status_code == 202
        body = response.get_json()
        assert body["kind"] == "jellyfin"
        proc.assert_called_once()
        assert proc.call_args.kwargs["item_id_by_server"] == {"jelly-1": "jf-42"}

    def test_irrelevant_event_returns_202(self, client, auth_headers):
        _seed_servers(
            [
                {
                    "id": "jelly-1",
                    "type": "jellyfin",
                    "name": "Jellyfin",
                    "enabled": True,
                    "url": "http://jellyfin:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                }
            ]
        )

        response = client.post(
            "/api/webhooks/incoming",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps({"NotificationType": "PlaybackStart", "ItemId": "x", "ServerId": "jelly-1"}),
        )

        assert response.status_code == 202
        assert response.get_json()["status"] == "ignored"

    def test_unresolvable_item_returns_202(self, client, auth_headers, monkeypatch):
        """Item lookup returns no path → 202 ignored, no dispatch."""
        _seed_servers(
            [
                {
                    "id": "jelly-1",
                    "type": "jellyfin",
                    "name": "Jellyfin",
                    "enabled": True,
                    "url": "http://jellyfin:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "libraries": [{"id": "1", "name": "TV", "remote_paths": ["/data/tv"], "enabled": True}],
                }
            ]
        )

        from media_preview_generator.servers.jellyfin import JellyfinServer

        monkeypatch.setattr(
            JellyfinServer,
            "resolve_item_to_remote_path",
            lambda self, item_id: None,
        )

        with patch("media_preview_generator.web.webhook_router.create_vendor_webhook_job") as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "NotificationType": "ItemAdded",
                        "ItemId": "missing",
                        "ItemType": "Episode",
                        "ServerId": "jelly-1",
                    }
                ),
            )

        assert response.status_code == 202
        proc.assert_not_called()

    def test_path_outside_configured_libraries_still_dispatches_via_hint(self, client, auth_headers, monkeypatch):
        """Vendor hint is authoritative even when the path's outside library roots.

        Background: a Jellyfin webhook always arrives with an item-id hint
        ({server_id: item_id}). We trust that hint over the registry's
        library-prefix matcher — Jellyfin clearly believes the item lives on
        that server, and the legacy ``_resolve_publishers`` already accepts
        hints as authoritative (see processing/multi_server.py lines 201-207).

        So a webhook with a hint always creates a Job; if the dispatcher
        decides there are no real owners after hitting the server, that's
        the Job's outcome — not something the pre-flight check rejects.
        Skipping at the pre-flight stage would silently drop legitimate
        webhooks for paths outside the registry's known roots.
        """
        _seed_servers(
            [
                {
                    "id": "jelly-1",
                    "type": "jellyfin",
                    "name": "Jellyfin",
                    "enabled": True,
                    "url": "http://jellyfin:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "libraries": [{"id": "1", "name": "TV", "remote_paths": ["/data/tv"], "enabled": True}],
                }
            ]
        )

        from media_preview_generator.servers.jellyfin import JellyfinServer

        monkeypatch.setattr(
            JellyfinServer,
            "resolve_item_to_remote_path",
            lambda self, item_id: "/somewhere/else/foo.mkv",
        )

        with patch(
            "media_preview_generator.web.webhook_router.create_vendor_webhook_job",
            return_value="job-fake-no-owners",
        ) as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "NotificationType": "ItemAdded",
                        "ItemId": "jf-99",
                        "ItemType": "Movie",
                        "ServerId": "jelly-1",
                    }
                ),
            )

        assert response.status_code == 202
        body = response.get_json()
        assert body["status"] == "queued"
        # Hint is authoritative — webhook does create a job; the dispatcher
        # inside the job is responsible for the NO_OWNERS verdict if the
        # path turns out to be unpublishable on every owning server.
        proc.assert_called_once()

    def test_resolution_exception_returns_202(self, client, auth_headers, monkeypatch):
        """Vendor server raising during item lookup degrades gracefully."""
        _seed_servers(
            [
                {
                    "id": "jelly-1",
                    "type": "jellyfin",
                    "name": "Jellyfin",
                    "enabled": True,
                    "url": "http://jellyfin:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "libraries": [{"id": "1", "name": "TV", "remote_paths": ["/data/tv"], "enabled": True}],
                }
            ]
        )

        from media_preview_generator.servers.jellyfin import JellyfinServer

        def boom(self, item_id):
            raise RuntimeError("jellyfin api unreachable")

        monkeypatch.setattr(JellyfinServer, "resolve_item_to_remote_path", boom)

        with patch("media_preview_generator.web.webhook_router.create_vendor_webhook_job") as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "NotificationType": "ItemAdded",
                        "ItemId": "jf-1",
                        "ItemType": "Episode",
                        "ServerId": "jelly-1",
                    }
                ),
            )

        # Accepted but ignored — the webhook is well-formed, the failure is
        # transient on the vendor side. The user's webhook plugin shouldn't
        # interpret this as needing a retry storm.
        assert response.status_code == 202
        proc.assert_not_called()

    def test_path_mapping_applied_to_jellyfin_payload(self, client, auth_headers, monkeypatch):
        """The configured ``path_mappings`` translate Jellyfin remote path → local."""
        _seed_servers(
            [
                {
                    "id": "jelly-1",
                    "type": "jellyfin",
                    "name": "Jellyfin",
                    "enabled": True,
                    "url": "http://jellyfin:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "libraries": [
                        {
                            "id": "1",
                            "name": "TV",
                            "remote_paths": ["/jf/data/tv"],
                            "enabled": True,
                        }
                    ],
                    "path_mappings": [{"remote_prefix": "/jf/data", "local_prefix": "/local/data"}],
                }
            ]
        )

        from media_preview_generator.servers.jellyfin import JellyfinServer

        monkeypatch.setattr(
            JellyfinServer,
            "resolve_item_to_remote_path",
            lambda self, item_id: "/jf/data/tv/Foo/S01E01.mkv",
        )

        with patch(
            "media_preview_generator.web.webhook_router.create_vendor_webhook_job",
            return_value="job-fake-12345678",
        ) as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "NotificationType": "ItemAdded",
                        "ItemId": "jf-1",
                        "ItemType": "Episode",
                        "ServerId": "jelly-1",
                    }
                ),
            )

        assert response.status_code == 202
        proc.assert_called_once()
        assert proc.call_args.kwargs["canonical_path"] == "/local/data/tv/Foo/S01E01.mkv"


class TestEmbyWebhook:
    def test_library_new_event(self, client, auth_headers, monkeypatch):
        _seed_servers(
            [
                {
                    "id": "emby-1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/em/movies"], "enabled": True}],
                    "path_mappings": [{"remote_prefix": "/em", "local_prefix": "/data"}],
                }
            ]
        )

        from media_preview_generator.servers.emby import EmbyServer

        monkeypatch.setattr(
            EmbyServer,
            "resolve_item_to_remote_path",
            lambda self, item_id: "/em/movies/Foo.mkv",
        )

        with patch(
            "media_preview_generator.web.webhook_router.create_vendor_webhook_job",
            return_value="job-fake-12345678",
        ) as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "Event": "library.new",
                        "Item": {"Id": "em-42"},
                        "Server": {"Id": "emby-1"},
                    }
                ),
            )

        assert response.status_code == 202
        body = response.get_json()
        assert body["kind"] == "emby"
        proc.assert_called_once()
        # Path mapping translated /em/movies/Foo.mkv -> /data/movies/Foo.mkv.
        assert proc.call_args.kwargs["canonical_path"] == "/data/movies/Foo.mkv"

    def test_irrelevant_event_returns_202(self, client, auth_headers):
        """Emby's playback events shouldn't trigger preview work."""
        _seed_servers(
            [
                {
                    "id": "emby-1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                }
            ]
        )

        response = client.post(
            "/api/webhooks/incoming",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps(
                {
                    "Event": "playback.start",
                    "Item": {"Id": "x"},
                    "Server": {"Id": "emby-1"},
                }
            ),
        )

        assert response.status_code == 202
        assert response.get_json()["status"] == "ignored"

    def test_unresolvable_item_returns_202(self, client, auth_headers, monkeypatch):
        """Emby item resolution returning None → 202 with no dispatch."""
        _seed_servers(
            [
                {
                    "id": "emby-1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/em/movies"], "enabled": True}],
                }
            ]
        )

        from media_preview_generator.servers.emby import EmbyServer

        monkeypatch.setattr(
            EmbyServer,
            "resolve_item_to_remote_path",
            lambda self, item_id: None,
        )

        with patch("media_preview_generator.web.webhook_router.create_vendor_webhook_job") as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "Event": "library.new",
                        "Item": {"Id": "missing"},
                        "Server": {"Id": "emby-1"},
                    }
                ),
            )

        assert response.status_code == 202
        proc.assert_not_called()

    def test_resolution_exception_returns_202(self, client, auth_headers, monkeypatch):
        """Vendor server raising during item lookup degrades gracefully on Emby."""
        _seed_servers(
            [
                {
                    "id": "emby-1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/em/movies"], "enabled": True}],
                }
            ]
        )

        from media_preview_generator.servers.emby import EmbyServer

        def boom(self, item_id):
            raise RuntimeError("emby api unreachable")

        monkeypatch.setattr(EmbyServer, "resolve_item_to_remote_path", boom)

        with patch("media_preview_generator.web.webhook_router.create_vendor_webhook_job") as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "Event": "library.new",
                        "Item": {"Id": "em-1"},
                        "Server": {"Id": "emby-1"},
                    }
                ),
            )

        assert response.status_code == 202
        proc.assert_not_called()

    def test_two_emby_servers_route_by_server_id(self, client, auth_headers, monkeypatch):
        """Two Emby servers configured — payload's ``Server.Id`` picks the right one."""
        _seed_servers(
            [
                {
                    "id": "uuid-emby-A",
                    "type": "emby",
                    "name": "Emby A",
                    "enabled": True,
                    "url": "http://a:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "server_identity": "emby-id-A",
                    "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/data/movies"], "enabled": True}],
                },
                {
                    "id": "uuid-emby-B",
                    "type": "emby",
                    "name": "Emby B",
                    "enabled": True,
                    "url": "http://b:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "server_identity": "emby-id-B",
                    "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/data/movies"], "enabled": True}],
                },
            ]
        )

        from media_preview_generator.servers.emby import EmbyServer

        monkeypatch.setattr(
            EmbyServer,
            "resolve_item_to_remote_path",
            lambda self, item_id: "/data/movies/Foo.mkv",
        )

        with patch(
            "media_preview_generator.web.webhook_router.create_vendor_webhook_job",
            return_value="job-fake-12345678",
        ) as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "Event": "library.new",
                        "Item": {"Id": "em-42"},
                        "Server": {"Id": "emby-id-B"},
                    }
                ),
            )

        assert response.status_code == 202
        proc.assert_called_once()
        # Hint dict only carries the matched server's local id.
        assert proc.call_args.kwargs["item_id_by_server"] == {"uuid-emby-B": "em-42"}


class TestPlexWebhook:
    """D31 — assert that for a Plex-native multipart webhook, the resulting
    ``item_id_by_server`` value is the BARE ratingKey, NOT the URL form
    ``/library/metadata/<id>``. This is the router-layer site of the D31
    bug: if ``parse_webhook`` ever again stores the URL form here, downstream
    ``PlexBundleAdapter.compute_output_paths`` builds
    ``/library/metadata//library/metadata/<id>/tree`` → 404 → silent
    "skipped_not_indexed" lie on every Sonarr/Radarr → Plex webhook for days.
    """

    def _seed_plex_server(self):
        _seed_servers(
            [
                {
                    "id": "plex-1",
                    "type": "plex",
                    "name": "Plex",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {"token": "tok"},
                    "libraries": [
                        {"id": "1", "name": "Movies", "remote_paths": ["/data/movies"], "enabled": True},
                    ],
                }
            ]
        )

    def test_item_id_by_server_is_bare_rating_key(self, client, auth_headers, monkeypatch):
        """Plex webhook -> router stores BARE ratingKey in item_id_by_server."""
        from media_preview_generator.servers.plex import PlexServer

        self._seed_plex_server()
        # Stub the path-resolution so we can assert on the item_id without
        # caring how Plex would actually resolve the file.
        monkeypatch.setattr(
            PlexServer,
            "resolve_item_to_remote_path",
            lambda self, item_id: "/data/movies/Foo.mkv",
        )

        plex_payload = {
            "event": "library.new",
            "Metadata": {
                "ratingKey": "557676",
                "title": "Foo",
                "type": "movie",
            },
        }
        with patch(
            "media_preview_generator.web.webhook_router.create_vendor_webhook_job",
            return_value="job-fake-12345678",
        ) as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers=auth_headers,
                data={"payload": json.dumps(plex_payload)},
                content_type="multipart/form-data",
            )

        assert response.status_code == 202, response.get_data(as_text=True)
        assert response.get_json()["kind"] == "plex"
        proc.assert_called_once()
        item_id_by_server = proc.call_args.kwargs["item_id_by_server"]
        assert item_id_by_server == {"plex-1": "557676"}, (
            f"D31 regression — expected bare ratingKey, got {item_id_by_server!r}. "
            "URL form like '/library/metadata/557676' would double-prefix downstream."
        )
        bare_id = item_id_by_server["plex-1"]
        assert "/" not in bare_id, (
            f"item_id contains '/' ({bare_id!r}) — would yield '/library/metadata//library/metadata/...' on /tree query"
        )
        assert not bare_id.startswith("/library/metadata/"), "item_id is in URL form — D31 root-cause shape"

    def test_non_library_new_event_returns_202(self, client, auth_headers):
        """Playback-state events (media.play etc.) must NOT dispatch."""
        self._seed_plex_server()
        plex_payload = {"event": "media.play", "Metadata": {"ratingKey": "1"}}
        with patch(
            "media_preview_generator.web.webhook_router.create_vendor_webhook_job",
            return_value="job-fake-12345678",
        ) as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers=auth_headers,
                data={"payload": json.dumps(plex_payload)},
                content_type="multipart/form-data",
            )

        # router treats parse_webhook -> None as "irrelevant", returning 202 not 200.
        assert response.status_code == 202, response.get_data(as_text=True)
        proc.assert_not_called()


class TestPathFirstWebhook:
    def test_simple_path_dispatch(self, client, auth_headers):
        # No pre-flight check anymore — every webhook becomes a Job.
        with patch(
            "media_preview_generator.web.webhook_router.create_vendor_webhook_job",
            return_value="job-fake-12345678",
        ) as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps({"path": "/data/tv/Show/S01E01.mkv"}),
            )

        assert response.status_code == 202
        body = response.get_json()
        assert body["kind"] == "path"
        proc.assert_called_once()


class TestWebhookPrefixTranslationReachesOwnerCheck:
    """TEST_AUDIT P0.4 — closes incident 70275e9 silent 202 drop class.

    Background: the original ``no_owners`` pre-flight check ran against
    the RAW webhook payload path WITHOUT applying ``webhook_prefixes``.
    Sonarr installs that translate ``/data/tv → /mnt/data/tv`` got 202s
    silently because the pre-flight thought no server owned ``/data/tv``
    (true; the local owner is ``/mnt/data/tv``). The fix removed the
    pre-flight entirely; the orchestrator (which DOES apply prefixes via
    ``_log_webhook_owning_servers``) now decides ownership inside the
    Job.

    These tests pin the post-fix behaviour: every webhook payload
    becomes a Job, regardless of whether the path appears to be owned
    or not. A regression that re-introduces a path-based pre-flight
    would fail the first test loudly.
    """

    def test_webhook_with_remote_form_path_and_prefix_mapping_creates_job(self, client, auth_headers):
        """Server has ``webhook_prefixes=[("/data/tv", "/mnt/data/tv")]``;
        webhook arrives with raw ``/data/tv/...`` path. Must create a Job
        — pre-fix would have 202'd with ``ignored_no_owners`` because the
        path didn't match the local prefix.

        Asserts on the response shape (kind=path, status=queued, job_id
        present) AND that ``create_vendor_webhook_job`` was called with the
        exact remote-form canonical path. The orchestrator handles the
        translation downstream — the router's job is to NOT drop.
        """
        # Pre-seed a server with a prefix mapping that would have triggered
        # the old pre-flight bug. We don't even need to verify the mapping
        # is applied here (orchestrator's job); we just need to confirm the
        # router doesn't 202-drop based on path inspection.
        from media_preview_generator.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.update(
            {
                "media_servers": [
                    {
                        "id": "plex-1",
                        "type": "plex",
                        "name": "Plex",
                        "enabled": True,
                        "url": "http://plex:32400",
                        "auth": {"token": "tok"},
                        "libraries": [{"id": "1", "name": "TV", "enabled": True}],
                        "path_mappings": [
                            {
                                "remote_prefix": "/data/tv",
                                "local_prefix": "/mnt/data/tv",
                                "webhook_prefixes": ["/data/tv"],
                            }
                        ],
                        "exclude_paths": [],
                    }
                ],
            }
        )

        with patch(
            "media_preview_generator.web.webhook_router.create_vendor_webhook_job",
            return_value="job-fake-87654321",
        ) as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps({"path": "/data/tv/Show/S01E01.mkv"}),
            )

        assert response.status_code == 202, (
            f"Webhook with remote-form path + prefix mapping must create a Job (status 202 queued), "
            f"NOT a silent ignored_no_owners drop. Got {response.status_code}; "
            f"body: {response.get_json()!r}. The 70275e9 silent-drop bug class would surface as "
            f"'ignored_no_owners' status with the fix reverted."
        )
        body = response.get_json()
        assert body["status"] == "queued", (
            f"Expected status='queued' (Job created); got {body.get('status')!r}. "
            f"'ignored_no_owners' here = the pre-flight regression has returned."
        )
        assert "job_id" in body and body["job_id"]
        assert body["kind"] == "path"

        # The router must have called create_vendor_webhook_job with the
        # raw remote-form path — translation happens downstream.
        proc.assert_called_once()
        kwargs = proc.call_args.kwargs
        assert kwargs.get("canonical_path") == "/data/tv/Show/S01E01.mkv", (
            f"Router should pass the raw payload path to the orchestrator; "
            f"got canonical_path={kwargs.get('canonical_path')!r}"
        )

    def test_webhook_with_unrecognised_path_still_creates_job_no_silent_drop(self, client, auth_headers):
        """Even when no server's path_mappings match, the router must still
        create a Job. The orchestrator will log a clear "no owners" message
        in the Job log (visible to user), NOT a silent 202 that the user
        only finds when nothing happens.

        Pre-fix: this scenario was ``ignored_no_owners`` — the user got
        no Job in the UI and no clear feedback that the webhook was
        dropped. Post-fix: a Job appears + the user can read its log to
        see exactly why no publishers fired.
        """
        from media_preview_generator.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.update(
            {
                "media_servers": [
                    {
                        "id": "plex-1",
                        "type": "plex",
                        "name": "Plex",
                        "enabled": True,
                        "url": "http://plex:32400",
                        "auth": {"token": "tok"},
                        "libraries": [{"id": "1", "name": "TV", "enabled": True}],
                        "path_mappings": [
                            {
                                "remote_prefix": "/somewhere/else",
                                "local_prefix": "/mnt/elsewhere",
                            }
                        ],
                        "exclude_paths": [],
                    }
                ],
            }
        )

        with patch(
            "media_preview_generator.web.webhook_router.create_vendor_webhook_job",
            return_value="job-fake-99999999",
        ) as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps({"path": "/random/path/x.mkv"}),
            )

        # Job IS created — the 202 is "queued", NOT "ignored_no_owners".
        assert response.status_code == 202
        body = response.get_json()
        assert body["status"] == "queued", (
            f"Even with no matching server, the router must queue a Job for visibility "
            f"(orchestrator decides ownership inside the Job log). Got {body.get('status')!r}."
        )
        proc.assert_called_once()


class TestUnknownPayload:
    def test_returns_400(self, client, auth_headers):
        response = client.post(
            "/api/webhooks/incoming",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps({"random": "noise"}),
        )
        assert response.status_code == 400


class TestPerServerFallback:
    def test_returns_404_for_unconfigured_server(self, client, auth_headers):
        _seed_servers([])
        response = client.post(
            "/api/webhooks/server/nope",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps({"path": "/x.mkv"}),
        )
        assert response.status_code == 404

    def test_per_server_url_pins_dispatch_to_one_server(self, client, auth_headers):
        """B2: posting to /api/webhooks/server/<id> must scope dispatch to
        only that server, even when sibling servers also own the path.

        Without ``server_id_filter`` a Plex+Jellyfin install would publish
        twice for the same webhook — surprising given the URL's intent.
        """
        _seed_servers(
            [
                {
                    "id": "plex-A",
                    "type": "plex",
                    "name": "Plex A",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {"token": "tok"},
                    "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/data/movies"], "enabled": True}],
                },
                {
                    "id": "jelly-1",
                    "type": "jellyfin",
                    "name": "Jellyfin",
                    "enabled": True,
                    "url": "http://jelly:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/data/movies"], "enabled": True}],
                },
            ]
        )

        with patch(
            "media_preview_generator.web.webhook_router.create_vendor_webhook_job",
            return_value="job-fake-12345678",
        ) as proc:
            response = client.post(
                "/api/webhooks/server/plex-A",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps({"path": "/data/movies/Foo.mkv"}),
            )

        assert response.status_code == 202
        proc.assert_called_once()
        assert proc.call_args.kwargs["server_id_filter"] == "plex-A"

    def test_universal_url_does_not_pin_dispatch(self, client, auth_headers):
        """Sanity: the universal /api/webhooks/incoming endpoint should
        NOT pass a ``server_id_filter`` — that's the per-server URL's job.
        """
        _seed_servers(
            [
                {
                    "id": "plex-A",
                    "type": "plex",
                    "name": "Plex A",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {"token": "tok"},
                    "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/data/movies"], "enabled": True}],
                },
            ]
        )

        with patch(
            "media_preview_generator.web.webhook_router.create_vendor_webhook_job",
            return_value="job-fake-12345678",
        ) as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps({"path": "/data/movies/Foo.mkv"}),
            )

        assert response.status_code == 202
        proc.assert_called_once()
        assert proc.call_args.kwargs.get("server_id_filter") is None


class TestServerIdentityRouting:
    """Verify multi-server-of-same-vendor routing uses the captured
    ``server_identity`` rather than the locally-generated UUID.

    Two Jellyfin servers configured; only the one whose
    ``server_identity`` matches the inbound payload's ``ServerId``
    should receive the dispatch.
    """

    def test_inbound_jellyfin_routes_to_matching_identity(self, client, auth_headers, monkeypatch):
        _seed_servers(
            [
                {
                    "id": "uuid-jelly-A",
                    "type": "jellyfin",
                    "name": "Jellyfin A",
                    "enabled": True,
                    "url": "http://a:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "server_identity": "jf-server-id-A",
                    "libraries": [{"id": "1", "name": "TV", "remote_paths": ["/data/tv"], "enabled": True}],
                },
                {
                    "id": "uuid-jelly-B",
                    "type": "jellyfin",
                    "name": "Jellyfin B",
                    "enabled": True,
                    "url": "http://b:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "server_identity": "jf-server-id-B",
                    "libraries": [{"id": "1", "name": "TV", "remote_paths": ["/data/tv"], "enabled": True}],
                },
            ]
        )

        from media_preview_generator.servers.jellyfin import JellyfinServer

        monkeypatch.setattr(
            JellyfinServer,
            "resolve_item_to_remote_path",
            lambda self, item_id: "/data/tv/Show/S01E01.mkv",
        )

        with patch(
            "media_preview_generator.web.webhook_router.create_vendor_webhook_job",
            return_value="job-fake-12345678",
        ) as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "NotificationType": "ItemAdded",
                        "ItemId": "jf-42",
                        "ItemType": "Episode",
                        "ServerId": "jf-server-id-B",
                    }
                ),
            )

        assert response.status_code == 202
        proc.assert_called_once()
        # Only the matching server's id should appear in the hint dict.
        hints = proc.call_args.kwargs["item_id_by_server"]
        assert hints == {"uuid-jelly-B": "jf-42"}

    def test_inbound_with_unknown_identity_when_multiple_configured(self, client, auth_headers):
        _seed_servers(
            [
                {
                    "id": "uuid-jelly-A",
                    "type": "jellyfin",
                    "name": "Jellyfin A",
                    "enabled": True,
                    "url": "http://a:8096",
                    "auth": {},
                    "server_identity": "jf-server-id-A",
                },
                {
                    "id": "uuid-jelly-B",
                    "type": "jellyfin",
                    "name": "Jellyfin B",
                    "enabled": True,
                    "url": "http://b:8096",
                    "auth": {},
                    "server_identity": "jf-server-id-B",
                },
            ]
        )

        response = client.post(
            "/api/webhooks/incoming",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps(
                {
                    "NotificationType": "ItemAdded",
                    "ItemId": "jf-42",
                    "ItemType": "Episode",
                    "ServerId": "jf-server-id-UNKNOWN",
                }
            ),
        )

        # No identity matches; ambiguous fallback (>1 candidate of this
        # type) → router can't safely pick one. The handler returns 202
        # for "could not match a configured server" rather than 200.
        # Audit fix — also assert the body explicitly says "ignored" so a
        # regression that silently routed to server A wouldn't pass.
        assert response.status_code == 202
        body = response.get_json() or {}
        assert body.get("status") == "ignored", (
            f"router silently picked a server despite ambiguous identity — body={body!r}"
        )

    def test_identity_collision_refuses_to_route(self, client, auth_headers):
        """Two configured servers share the same ``server_identity``
        (cloned-VM scenario). Router must refuse rather than silently
        picking the first match — the dispatch could go to the wrong
        server's plex_config_folder and never appear in the user's UI."""
        _seed_servers(
            [
                {
                    "id": "uuid-jelly-A",
                    "type": "jellyfin",
                    "name": "Jellyfin A",
                    "enabled": True,
                    "url": "http://a:8096",
                    "auth": {},
                    # SAME server_identity as B (cloned VM).
                    "server_identity": "jf-cloned-id",
                },
                {
                    "id": "uuid-jelly-B",
                    "type": "jellyfin",
                    "name": "Jellyfin B",
                    "enabled": True,
                    "url": "http://b:8096",
                    "auth": {},
                    "server_identity": "jf-cloned-id",
                },
            ]
        )

        response = client.post(
            "/api/webhooks/incoming",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps(
                {
                    "NotificationType": "ItemAdded",
                    "ItemId": "jf-42",
                    "ItemType": "Episode",
                    "ServerId": "jf-cloned-id",
                }
            ),
        )

        # Router refuses to route under collision. Same 202 path as
        # "no match found" since the user-visible outcome is the same:
        # use the per-server fallback URL or fix one server's identity.
        # Audit fix — also assert "ignored" body status. Without this, a
        # regression that silently picked the FIRST cloned server would
        # pass: 202 is also returned by the queued path. Distinguish.
        assert response.status_code == 202
        body = response.get_json() or {}
        assert body.get("status") == "ignored", (
            f"router silently picked a server despite identity collision — body={body!r}"
        )


class TestAuth:
    def test_missing_token_rejected(self, client):
        response = client.post(
            "/api/webhooks/incoming",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"path": "/x.mkv"}),
        )
        # webhooks._authenticate_webhook returns 401 on missing token (never 403);
        # tightened from `in (401, 403)` so a regression that flips to a different
        # status (e.g. silent 200) can't slip through.
        assert response.status_code == 401, response.status_code
        body = response.get_json() or {}
        assert "Authentication required" in (body.get("error") or "")


class TestPayloadSizeLimit:
    """Webhook bodies cap at MAX_CONTENT_LENGTH (1 MiB) to thwart DoS."""

    def test_oversized_payload_returns_413(self, client, auth_headers):
        # 2 MiB JSON blob — well over the 1 MiB cap.
        oversized = json.dumps({"path": "/x.mkv", "junk": "A" * (2 * 1024 * 1024)})
        response = client.post(
            "/api/webhooks/incoming",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=oversized,
        )
        assert response.status_code == 413, response.status_code

    def test_normal_size_payload_is_accepted(self, client, auth_headers):
        """Sanity: a small (real-world-sized) payload still gets through.

        Every webhook now lands as a Job — even ones for paths no server
        currently owns. That's deliberate (the orchestrator handles the
        no-owners outcome and writes a visible row to the Jobs UI rather
        than the router silently 202'ing). Mock the job-creation entry
        point so this test asserts the routing decision (status=queued)
        without spinning up a real worker thread.

        Tightened from `in (200, 202)` so a regression that returns the
        wrong code is caught immediately.
        """
        normal = json.dumps({"path": "/data/movies/Test/Test.mkv"})
        with patch(
            "media_preview_generator.web.webhook_router.create_vendor_webhook_job",
            return_value="job-fake-12345678",
        ):
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=normal,
            )
        assert response.status_code == 202, response.status_code
        body = response.get_json() or {}
        assert body.get("status") == "queued", body
