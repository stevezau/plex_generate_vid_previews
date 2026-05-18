"""Regression: a native Plex ``library.new`` webhook fired by Plex-B
must resolve the ratingKey against Plex-B, not media_servers[0].

This is the multi-Plex shadow of issue #244 in the webhook flow. The
library-scan path (orchestrator gate) was the user-facing report; this
file pins the second half — when Plex's payload omits file paths under
``Metadata.Media[].Part[].file`` and the resolver has to look the item
up by ratingKey, the lookup MUST happen on the originating Plex (the
one Plex sent the webhook from). Pre-fix, ``_resolve_plex_paths_from_rating_key``
called ``load_config()`` → ``derive_legacy_plex_view(...)`` with no
``server_id`` argument, so it always projected ``media_servers[0]``'s
URL/token — guaranteed to return ``[]`` for a Plex-B-only ratingKey.

The router has the information needed to disambiguate: Plex's webhook
payload carries ``Server.uuid`` (the source server's machineIdentifier).
``servers.registry.server_config_from_dict`` already stores each
configured Plex's ``server_identity`` from the test-connection probe,
so the webhook handler can match the inbound uuid to a media_servers[]
entry and forward that entry's id to the resolver.
"""

from __future__ import annotations

import io
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.web.app import create_app
from media_preview_generator.web.settings_manager import reset_settings_manager


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Mirror the reset fixture from test_webhooks_plex.py."""
    reset_settings_manager()
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    import media_preview_generator.web.scheduler as sched_mod

    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    from media_preview_generator.web.routes import clear_gpu_cache

    clear_gpu_cache()
    import media_preview_generator.web.webhooks as wh

    wh._webhook_history.clear()
    with wh._pending_lock:
        for t in wh._pending_timers.values():
            t.cancel()
        wh._pending_timers.clear()
        wh._pending_batches.clear()
        wh._recent_dispatches.clear()
    yield
    reset_settings_manager()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        if sched_mod._schedule_manager is not None:
            try:
                sched_mod._schedule_manager.stop()
            except Exception:
                pass
            sched_mod._schedule_manager = None
    clear_gpu_cache()
    wh._webhook_history.clear()
    with wh._pending_lock:
        for t in wh._pending_timers.values():
            t.cancel()
        wh._pending_timers.clear()
        wh._pending_batches.clear()
        wh._recent_dispatches.clear()


@pytest.fixture()
def app(tmp_path):
    """Create a Flask app pinned to a tmp ``CONFIG_DIR``.

    media_servers is NOT written to the on-disk settings.json — instead
    the integration tests set it via the live ``settings_manager`` after
    create_app. Writing it to disk via the fixture risked the
    schema-upgrade pipeline persisting it back to ``/config`` in some
    failure modes, polluting the dev box's real config.
    """
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir, exist_ok=True)

    auth_file = os.path.join(config_dir, "auth.json")
    with open(auth_file, "w") as f:
        json.dump({"token": "test-token-12345678"}, f)

    settings_file = os.path.join(config_dir, "settings.json")
    with open(settings_file, "w") as f:
        json.dump(
            {
                "setup_complete": True,
                "webhook_enabled": True,
            },
            f,
        )

    with patch.dict(
        os.environ,
        {
            "CONFIG_DIR": config_dir,
            "WEB_AUTH_TOKEN": "test-token-12345678",
            "WEB_PORT": "8099",
        },
    ):
        flask_app = create_app(config_dir=config_dir)
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        yield flask_app


@pytest.fixture()
def two_plex_servers():
    """Inject a two-Plex ``media_servers`` array via the live singleton
    AFTER create_app has bound it to the tmp config dir. Returns the
    raw list so tests can mutate it before posting if needed."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    entries = [
        {
            "id": "plex-a",
            "type": "plex",
            "enabled": True,
            "server_identity": "mid-a",
            "url": "http://plex-a:32400",
            "auth": {"token": "tok-a"},
        },
        {
            "id": "plex-b",
            "type": "plex",
            "enabled": True,
            "server_identity": "mid-b",
            "url": "http://plex-b:32400",
            "auth": {"token": "tok-b"},
        },
    ]
    get_settings_manager().set("media_servers", entries)
    return entries


@pytest.fixture()
def client(app):
    return app.test_client()


def _multipart_post(client, payload_dict, query: str = ""):
    """POST a Plex-style multipart/form-data request to /api/webhooks/plex."""
    return client.post(
        f"/api/webhooks/plex{query}",
        data={"payload": (io.BytesIO(json.dumps(payload_dict).encode()), "payload.json")},
        content_type="multipart/form-data",
        headers={"X-Auth-Token": "test-token-12345678"},
    )


class TestResolverAcceptsServerIdArg:
    """Unit-level: the resolver must take a ``server_id`` kwarg and
    forward it into ``derive_legacy_plex_view`` so the projected
    ``plex_url`` / ``plex_token`` belong to the requested Plex, not
    ``media_servers[0]``."""

    def test_server_id_kwarg_flows_to_derive_legacy_plex_view(self):
        """When the caller passes ``server_id="plex-b"``, the resolver
        must call ``derive_legacy_plex_view`` with that exact id —
        otherwise the projected legacy view falls back to the first
        enabled Plex and the lookup misses every Plex-B-only ratingKey."""
        from media_preview_generator.web.webhooks import _resolve_plex_paths_from_rating_key

        mock_config = MagicMock()
        mock_config.plex_url = "http://plex-b:32400"
        mock_config.plex_token = "tok-b"

        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch(
                "media_preview_generator.config.load_config",
                return_value=mock_config,
            ),
            patch(
                "media_preview_generator.config.derive_legacy_plex_view",
                return_value={"plex_url": "http://plex-b:32400", "plex_token": "tok-b"},
            ) as mock_derive,
            patch(
                "media_preview_generator.plex_client.plex_server",
                return_value=MagicMock(fetchItem=MagicMock(side_effect=Exception("ratingKey not found"))),
            ),
        ):
            mock_sm.return_value.get.return_value = [
                {"id": "plex-a", "type": "plex", "enabled": True},
                {"id": "plex-b", "type": "plex", "enabled": True},
            ]
            _resolve_plex_paths_from_rating_key("777", server_id="plex-b")

        mock_derive.assert_called_once()
        assert mock_derive.call_args.kwargs.get("server_id") == "plex-b", (
            f"derive_legacy_plex_view must receive server_id='plex-b' so the "
            f"lookup hits Plex-B's URL/token, got kwargs={mock_derive.call_args.kwargs!r}"
        )

    def test_no_server_id_arg_preserves_existing_behaviour(self):
        """Control: omitting ``server_id`` must keep the historical
        ``media_servers[0]`` fallback so single-Plex installs and
        callers that haven't been updated yet keep working."""
        from media_preview_generator.web.webhooks import _resolve_plex_paths_from_rating_key

        mock_config = MagicMock()

        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch(
                "media_preview_generator.config.load_config",
                return_value=mock_config,
            ),
            patch(
                "media_preview_generator.config.derive_legacy_plex_view",
                return_value={},
            ) as mock_derive,
            patch(
                "media_preview_generator.plex_client.plex_server",
                return_value=MagicMock(fetchItem=MagicMock(side_effect=Exception("nope"))),
            ),
        ):
            mock_sm.return_value.get.return_value = [
                {"id": "plex-a", "type": "plex", "enabled": True},
            ]
            _resolve_plex_paths_from_rating_key("777")

        mock_derive.assert_called_once()
        assert mock_derive.call_args.kwargs.get("server_id") is None


class TestPlexWebhookRoutesToOriginatingServer:
    """Integration: a ``library.new`` POST carrying ``Server.uuid=mid-b``
    must trigger a ratingKey lookup against Plex-B, not Plex-A."""

    def test_uuid_match_routes_to_originating_plex(self, client, two_plex_servers):
        """Plex-B has machineIdentifier ``mid-b``. A webhook from Plex-B
        must cause the ratingKey resolver to be called with
        ``server_id="plex-b"`` — so the lookup uses Plex-B's URL/token
        instead of defaulting to ``media_servers[0]``."""
        payload = {
            "event": "library.new",
            "Server": {"title": "Plex", "uuid": "mid-b"},
            "Metadata": {"ratingKey": "777", "type": "movie", "title": "Test"},
        }

        with (
            patch(
                "media_preview_generator.web.webhooks._resolve_plex_paths_from_rating_key",
                return_value=(["/data/movies/Foo.mkv"], "Test Movie"),
            ) as mock_resolve,
            patch(
                "media_preview_generator.web.webhooks._schedule_webhook_job",
                return_value=True,
            ) as mock_schedule,
        ):
            resp = _multipart_post(client, payload)

        assert resp.status_code == 202, resp.get_data(as_text=True)
        mock_resolve.assert_called_once()
        # Bug-blind guard #1 (ratingKey resolver): assert the ACTUAL
        # server_id forwarded — not just that the resolver was called.
        assert mock_resolve.call_args.kwargs.get("server_id") == "plex-b", (
            f"resolver must receive server_id='plex-b' for a mid-b webhook, "
            f"got kwargs={mock_resolve.call_args.kwargs!r}"
        )
        # Bug-blind guard #2 (dispatch site): the Sonarr handler already
        # threads server_id into _schedule_webhook_job (webhooks.py:1325).
        # The Plex handler MUST do the same — without it (a) the Job UI's
        # source chip shows generic "plex" instead of "Plex-B", (b) the
        # dedup key ``(source, server_id, canonical_path)`` collapses
        # Plex-A and Plex-B events for the same path on the 60s window,
        # silently dropping the second one, and (c) the orchestrator gate
        # (`enabled_plex_count >= 2`) routes to multi-server fan-out with
        # no pin so the path gets processed against BOTH Plex servers
        # instead of just Plex-B. The HIGH finding from the architecture
        # review on issue #244 part 2.
        mock_schedule.assert_called()
        sched_kwargs = mock_schedule.call_args.kwargs
        assert sched_kwargs.get("server_id") == "plex-b", (
            f"dispatch must receive server_id='plex-b' so the Job is "
            f"pinned to the originating Plex; got kwargs={sched_kwargs!r}"
        )

    def test_unknown_uuid_falls_back_to_no_pin(self, client, two_plex_servers):
        """A payload whose ``Server.uuid`` matches no configured Plex
        must NOT crash and must NOT silently pin to media_servers[0].
        Falls back to ``server_id=None`` — the resolver then projects
        from the first enabled Plex (pre-fix behaviour, acceptable
        because there is no better signal to use)."""
        payload = {
            "event": "library.new",
            "Server": {"title": "Plex", "uuid": "mid-ghost"},
            "Metadata": {"ratingKey": "777", "type": "movie", "title": "Test"},
        }

        with (
            patch(
                "media_preview_generator.web.webhooks._resolve_plex_paths_from_rating_key",
                return_value=(["/data/movies/Foo.mkv"], "Test Movie"),
            ) as mock_resolve,
            patch("media_preview_generator.web.webhooks._schedule_webhook_job", return_value=True),
        ):
            resp = _multipart_post(client, payload)

        assert resp.status_code == 202, resp.get_data(as_text=True)
        mock_resolve.assert_called_once()
        assert mock_resolve.call_args.kwargs.get("server_id") is None, (
            f"unknown uuid must yield server_id=None (fall back, don't pin to "
            f"media_servers[0]), got {mock_resolve.call_args.kwargs!r}"
        )

    def test_disabled_pin_falls_through_with_warning(self, client, caplog):
        """A ``?server_id=`` pin pointing at a DISABLED Plex must not
        be silently honoured — ``derive_legacy_plex_view`` would then
        fail to match and return the first enabled Plex's view, the
        exact first-Plex ghost this fix exists to eliminate. The pin
        must be rejected and the resolver must fall through to
        payload-uuid matching (which here finds the enabled Plex)."""
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set(
            "media_servers",
            [
                {
                    "id": "plex-a",
                    "type": "plex",
                    "enabled": True,
                    "server_identity": "mid-a",
                    "url": "http://plex-a:32400",
                    "auth": {"token": "tok-a"},
                },
                {
                    "id": "plex-b-disabled",
                    "type": "plex",
                    "enabled": False,
                    "server_identity": "mid-b",
                    "url": "http://plex-b:32400",
                    "auth": {"token": "tok-b"},
                },
            ],
        )
        payload = {
            "event": "library.new",
            "Server": {"title": "Plex", "uuid": "mid-a"},
            "Metadata": {"ratingKey": "777", "type": "movie", "title": "Test"},
        }

        with (
            patch(
                "media_preview_generator.web.webhooks._resolve_plex_paths_from_rating_key",
                return_value=(["/data/movies/Foo.mkv"], "Test"),
            ) as mock_resolve,
            patch("media_preview_generator.web.webhooks._schedule_webhook_job", return_value=True),
        ):
            resp = _multipart_post(client, payload, query="?server_id=plex-b-disabled")

        assert resp.status_code == 202
        # Pin rejected → payload-uuid path took over → plex-a chosen.
        assert mock_resolve.call_args.kwargs.get("server_id") == "plex-a", (
            f"disabled pin must NOT be honoured; payload uuid should win, got {mock_resolve.call_args.kwargs!r}"
        )

    def test_non_plex_pin_falls_through_with_warning(self, client):
        """``?server_id=`` pointing at an Emby/Jellyfin entry by mistake
        must not be accepted. Same first-Plex-ghost concern as the
        disabled case."""
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set(
            "media_servers",
            [
                {
                    "id": "plex-a",
                    "type": "plex",
                    "enabled": True,
                    "server_identity": "mid-a",
                    "url": "http://plex-a:32400",
                    "auth": {"token": "tok-a"},
                },
                {"id": "emby-1", "type": "emby", "enabled": True, "url": "http://emby:8096"},
            ],
        )
        payload = {
            "event": "library.new",
            "Server": {"title": "Plex", "uuid": "mid-a"},
            "Metadata": {"ratingKey": "777", "type": "movie", "title": "Test"},
        }

        with (
            patch(
                "media_preview_generator.web.webhooks._resolve_plex_paths_from_rating_key",
                return_value=(["/data/movies/Foo.mkv"], "Test"),
            ) as mock_resolve,
            patch("media_preview_generator.web.webhooks._schedule_webhook_job", return_value=True),
        ):
            resp = _multipart_post(client, payload, query="?server_id=emby-1")

        assert resp.status_code == 202
        assert mock_resolve.call_args.kwargs.get("server_id") == "plex-a", (
            f"emby pin must NOT route the Plex webhook to emby; payload uuid should win, "
            f"got {mock_resolve.call_args.kwargs!r}"
        )

    def test_identity_collision_warns_and_falls_back(self, client, caplog):
        """Two Plex servers sharing the same machineIdentifier (cloned-
        VM edge case) — the resolver can't pick one, logs a warning,
        and returns None so the downstream lookup falls back to
        media_servers[0]'s view. Pin the warning fires so an operator
        can spot the collision in their logs."""
        import logging as _std_logging

        from loguru import logger as _loguru_logger

        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set(
            "media_servers",
            [
                {
                    "id": "plex-a",
                    "type": "plex",
                    "enabled": True,
                    "server_identity": "mid-shared",
                    "url": "http://plex-a:32400",
                    "auth": {"token": "tok-a"},
                },
                {
                    "id": "plex-b",
                    "type": "plex",
                    "enabled": True,
                    "server_identity": "mid-shared",
                    "url": "http://plex-b:32400",
                    "auth": {"token": "tok-b"},
                },
            ],
        )
        payload = {
            "event": "library.new",
            "Server": {"title": "Plex", "uuid": "mid-shared"},
            "Metadata": {"ratingKey": "777", "type": "movie", "title": "Test"},
        }

        # Bridge loguru → caplog so the warning is asserted.
        records: list = []
        handler_id = _loguru_logger.add(
            lambda msg: records.append(
                _std_logging.LogRecord(
                    name="loguru",
                    level=_std_logging.WARNING,
                    pathname="",
                    lineno=0,
                    msg=msg.record["message"],
                    args=(),
                    exc_info=None,
                )
            ),
            level="WARNING",
        )
        try:
            with (
                patch(
                    "media_preview_generator.web.webhooks._resolve_plex_paths_from_rating_key",
                    return_value=(["/data/movies/Foo.mkv"], "Test"),
                ) as mock_resolve,
                patch("media_preview_generator.web.webhooks._schedule_webhook_job", return_value=True),
            ):
                resp = _multipart_post(client, payload)
        finally:
            _loguru_logger.remove(handler_id)

        assert resp.status_code == 202
        assert mock_resolve.call_args.kwargs.get("server_id") is None, (
            f"identity collision must yield server_id=None (refuse-to-guess), got {mock_resolve.call_args.kwargs!r}"
        )
        # The operator's only signal that collision is happening — pin it.
        warning_text = " ".join(r.msg for r in records)
        assert "share the same" in warning_text and "machineIdentifier" in warning_text, (
            f"missing collision warning that tells the operator to fix it; got logs={warning_text!r}"
        )

    def test_empty_server_uuid_falls_back_to_no_pin(self, client, two_plex_servers):
        """``{"Server": {"uuid": ""}}`` is "no signal" not "match the
        empty-string identity". Must yield server_id=None."""
        payload = {
            "event": "library.new",
            "Server": {"title": "Plex", "uuid": ""},
            "Metadata": {"ratingKey": "777", "type": "movie", "title": "Test"},
        }
        with (
            patch(
                "media_preview_generator.web.webhooks._resolve_plex_paths_from_rating_key",
                return_value=(["/data/movies/Foo.mkv"], "Test"),
            ) as mock_resolve,
            patch("media_preview_generator.web.webhooks._schedule_webhook_job", return_value=True),
        ):
            resp = _multipart_post(client, payload)
        assert resp.status_code == 202
        assert mock_resolve.call_args.kwargs.get("server_id") is None

    def test_missing_server_block_falls_back_to_no_pin(self, client, two_plex_servers):
        """No ``Server`` key at all in the payload — fall back to None
        (single-Plex installs sometimes look like this)."""
        payload = {
            "event": "library.new",
            "Metadata": {"ratingKey": "777", "type": "movie", "title": "Test"},
        }
        with (
            patch(
                "media_preview_generator.web.webhooks._resolve_plex_paths_from_rating_key",
                return_value=(["/data/movies/Foo.mkv"], "Test"),
            ) as mock_resolve,
            patch("media_preview_generator.web.webhooks._schedule_webhook_job", return_value=True),
        ):
            resp = _multipart_post(client, payload)
        assert resp.status_code == 202
        assert mock_resolve.call_args.kwargs.get("server_id") is None

    def test_query_string_server_id_overrides_payload_uuid(self, client, two_plex_servers):
        """Operator pin: when ``?server_id=plex-b`` is in the URL, it
        wins over the payload's ``Server.uuid``. Mirrors the Sonarr
        webhook pattern at webhooks.py:1325 — explicit user intent
        beats payload heuristics."""
        payload = {
            "event": "library.new",
            "Server": {"title": "Plex", "uuid": "mid-a"},
            "Metadata": {"ratingKey": "777", "type": "movie", "title": "Test"},
        }

        with (
            patch(
                "media_preview_generator.web.webhooks._resolve_plex_paths_from_rating_key",
                return_value=(["/data/movies/Foo.mkv"], "Test Movie"),
            ) as mock_resolve,
            patch("media_preview_generator.web.webhooks._schedule_webhook_job", return_value=True),
        ):
            resp = _multipart_post(client, payload, query="?server_id=plex-b")

        assert resp.status_code == 202, resp.get_data(as_text=True)
        mock_resolve.assert_called_once()
        assert mock_resolve.call_args.kwargs.get("server_id") == "plex-b", (
            f"explicit ?server_id= must override payload uuid, got {mock_resolve.call_args.kwargs!r}"
        )
