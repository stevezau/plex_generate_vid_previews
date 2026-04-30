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
            "media_preview_generator.web.webhook_router.process_canonical_path",
            return_value=_published_result(),
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

        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "published"
        assert body["kind"] == "sonarr"
        proc.assert_called_once()
        assert proc.call_args.kwargs["canonical_path"] == "/data/tv/Foo/S01E01.mkv"

    def test_radarr_payload_classified_correctly(self, client, auth_headers):
        with patch(
            "media_preview_generator.web.webhook_router.process_canonical_path",
            return_value=_published_result(),
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

        assert response.status_code == 200
        assert response.get_json()["kind"] == "radarr"
        proc.assert_called_once()


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
            "media_preview_generator.web.webhook_router.process_canonical_path",
            return_value=_published_result(),
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

        assert response.status_code == 200
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
            "media_preview_generator.web.webhook_router.process_canonical_path",
            return_value=_published_result(),
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

        assert response.status_code == 200
        body = response.get_json()
        assert body["kind"] == "emby"
        proc.assert_called_once()
        # Path mapping translated /em/movies/Foo.mkv -> /data/movies/Foo.mkv.
        assert proc.call_args.kwargs["canonical_path"] == "/data/movies/Foo.mkv"


class TestPathFirstWebhook:
    def test_simple_path_dispatch(self, client, auth_headers):
        with patch(
            "media_preview_generator.web.webhook_router.process_canonical_path",
            return_value=_published_result(),
        ) as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps({"path": "/data/tv/Show/S01E01.mkv"}),
            )

        assert response.status_code == 200
        body = response.get_json()
        assert body["kind"] == "path"
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
            "media_preview_generator.web.webhook_router.process_canonical_path",
            return_value=_published_result(),
        ) as proc:
            response = client.post(
                "/api/webhooks/server/plex-A",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps({"path": "/data/movies/Foo.mkv"}),
            )

        assert response.status_code == 200
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
            "media_preview_generator.web.webhook_router.process_canonical_path",
            return_value=_published_result(),
        ) as proc:
            response = client.post(
                "/api/webhooks/incoming",
                headers={**auth_headers, "Content-Type": "application/json"},
                data=json.dumps({"path": "/data/movies/Foo.mkv"}),
            )

        assert response.status_code == 200
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
            "media_preview_generator.web.webhook_router.process_canonical_path",
            return_value=_published_result(),
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

        assert response.status_code == 200
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
        assert response.status_code == 202

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
        assert response.status_code == 202


class TestAuth:
    def test_missing_token_rejected(self, client):
        response = client.post(
            "/api/webhooks/incoming",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"path": "/x.mkv"}),
        )
        assert response.status_code in (401, 403)


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
        """Sanity: a small (real-world-sized) payload still gets through."""
        normal = json.dumps({"path": "/data/movies/Test/Test.mkv"})
        response = client.post(
            "/api/webhooks/incoming",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=normal,
        )
        # Either 200 (no_owners since no servers configured here) or
        # 202; never 413 / 401 for an authed in-bounds payload.
        assert response.status_code in (200, 202), response.status_code
