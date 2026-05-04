"""TEST_AUDIT P2.7 — webhook secret rotation re-registers Plex webhooks.

Closes a real but uncovered gap. When the user rotates ``webhook_secret``
in Settings, Plex Media Server needs the NEW token to authenticate its
webhooks. Without the post-save re-register hook,
``api_settings._reregister_plex_webhooks_after_secret_rotation`` never
fires and Plex keeps POSTing with the OLD token — the app silently
rejects every event afterwards.

Production wiring at ``api_settings.py:549-550``:

    if "webhook_secret" in updates:
        _reregister_plex_webhooks_after_secret_rotation(settings)

This file pins:
  1. The hook fires when ``webhook_secret`` is in the update dict
  2. Re-registration is attempted for EVERY enabled Plex server
  3. Non-Plex servers are skipped (Emby/Jellyfin webhooks don't carry
     the ``?token=`` query)
  4. Per-server failures are isolated — one Plex registration failure
     doesn't block the others or fail the settings save itself
  5. Servers without webhook_public_url are skipped (nothing to re-register)
  6. Servers without an auth token are skipped
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from media_preview_generator.web.app import create_app
from media_preview_generator.web.routes.api_settings import (
    _reregister_plex_webhooks_after_secret_rotation,
)
from media_preview_generator.web.settings_manager import (
    get_settings_manager,
    reset_settings_manager,
)


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_settings_manager()
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    import media_preview_generator.web.scheduler as sched_mod

    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
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


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_AUTH_TOKEN", "test-token-12345678")
    return create_app(config_dir=str(tmp_path))


def _seed_servers(media_servers: list[dict]) -> None:
    sm = get_settings_manager()
    sm.update({"media_servers": media_servers, "webhook_secret": "rotated-token"})


def _plex_entry(
    server_id: str, *, token: str = "plex-tok", public_url: str = "http://app:8080/api/webhooks/plex"
) -> dict:
    return {
        "id": server_id,
        "type": "plex",
        "name": f"Plex {server_id}",
        "enabled": True,
        "url": "http://plex:32400",
        "auth": {"token": token},
        "output": {"webhook_public_url": public_url},
    }


def _emby_entry(server_id: str) -> dict:
    return {
        "id": server_id,
        "type": "emby",
        "name": f"Emby {server_id}",
        "enabled": True,
        "url": "http://emby:8096",
        "auth": {"api_key": "emby-key"},
        "output": {"webhook_public_url": "http://app:8080/api/webhooks/emby"},
    }


# ---------------------------------------------------------------------------
# Direct unit tests of the post-save hook
# ---------------------------------------------------------------------------


class TestReregisterAfterSecretRotation:
    def test_re_registers_every_plex_server(self, app):
        with app.app_context():
            _seed_servers(
                [
                    _plex_entry("plex-a"),
                    _plex_entry("plex-b", token="other-tok", public_url="http://app:8080/api/webhooks/plex-b"),
                ]
            )

            with (
                patch("media_preview_generator.web.plex_webhook_registration.register") as mock_register,
                patch(
                    "media_preview_generator.web.routes.api_plex_webhook._plex_webhook_auth_token",
                    return_value="rotated-token",
                ),
            ):
                _reregister_plex_webhooks_after_secret_rotation(get_settings_manager())

        # Both Plex servers must have been re-registered with the NEW token.
        assert mock_register.call_count == 2, (
            f"Both configured Plex servers must be re-registered after secret rotation; "
            f"got {mock_register.call_count} call(s). The unregistered server will keep "
            f"posting with the old token and silently get 401-rejected."
        )
        # Per-call verify: the ROTATED token reaches the registration call.
        for call in mock_register.call_args_list:
            assert call.kwargs.get("auth_token") == "rotated-token", (
                f"Re-registration must pass the ROTATED token; got {call.kwargs.get('auth_token')!r}"
            )

    def test_skips_non_plex_servers(self, app):
        """Emby/Jellyfin webhooks don't carry ``?token=`` — they auth via
        ``X-Auth-Token`` header which is read FRESH per request. Re-registration
        is Plex-specific.
        """
        with app.app_context():
            _seed_servers([_emby_entry("emby-1"), _plex_entry("plex-1")])

            with (
                patch("media_preview_generator.web.plex_webhook_registration.register") as mock_register,
                patch(
                    "media_preview_generator.web.routes.api_plex_webhook._plex_webhook_auth_token",
                    return_value="rotated-token",
                ),
            ):
                _reregister_plex_webhooks_after_secret_rotation(get_settings_manager())

        assert mock_register.call_count == 1, (
            f"Only Plex servers should be re-registered; Emby/Jellyfin skipped. Got {mock_register.call_count} calls."
        )

    def test_skips_plex_server_without_webhook_public_url(self, app):
        """If the user hasn't configured a webhook_public_url for a Plex
        server, there's nothing to re-register — skip silently rather than
        raising.
        """
        with app.app_context():
            no_url = _plex_entry("plex-no-url")
            no_url["output"] = {}  # no webhook_public_url
            _seed_servers([no_url, _plex_entry("plex-with-url")])

            with (
                patch("media_preview_generator.web.plex_webhook_registration.register") as mock_register,
                patch(
                    "media_preview_generator.web.routes.api_plex_webhook._plex_webhook_auth_token",
                    return_value="rotated-token",
                ),
            ):
                _reregister_plex_webhooks_after_secret_rotation(get_settings_manager())

        assert mock_register.call_count == 1, (
            f"Plex server without webhook_public_url must be skipped; got {mock_register.call_count}"
        )

    def test_per_server_failure_does_not_block_other_servers(self, app):
        """One Plex server's re-registration failure (network down, 401, etc.)
        must NOT block the others. Each server is best-effort.
        """
        with app.app_context():
            _seed_servers([_plex_entry("plex-broken"), _plex_entry("plex-healthy")])

            call_count = {"n": 0}

            def maybe_fail(token, url, *, auth_token=None, server_id=None):
                call_count["n"] += 1
                if server_id == "plex-broken":
                    raise RuntimeError("network unreachable")

            with (
                patch(
                    "media_preview_generator.web.plex_webhook_registration.register",
                    side_effect=maybe_fail,
                ),
                patch(
                    "media_preview_generator.web.routes.api_plex_webhook._plex_webhook_auth_token",
                    return_value="rotated-token",
                ),
            ):
                # Must NOT raise — best-effort per server.
                _reregister_plex_webhooks_after_secret_rotation(get_settings_manager())

        assert call_count["n"] == 2, (
            f"Both servers must be ATTEMPTED even if one fails; got {call_count['n']} attempts. "
            f"A regression that bailed on first failure would leave plex-healthy with the old token."
        )

    def test_no_register_calls_when_no_plex_servers_configured(self, app):
        """Pure Emby/Jellyfin install — rotation is a no-op for the Plex side."""
        with app.app_context():
            _seed_servers([_emby_entry("emby-1")])

            with patch("media_preview_generator.web.plex_webhook_registration.register") as mock_register:
                _reregister_plex_webhooks_after_secret_rotation(get_settings_manager())

        assert mock_register.call_count == 0


# ---------------------------------------------------------------------------
# Wired-in test: POST /api/settings with new webhook_secret triggers the hook
# ---------------------------------------------------------------------------


class TestPostSettingsTriggersReregistration:
    """End-to-end: POST a new webhook_secret to /api/settings and assert the
    hook fires. Catches a regression where _apply_post_save_hooks stops
    calling the re-register function.
    """

    def test_post_webhook_secret_triggers_reregister_hook(self, app):
        client = app.test_client()

        with app.app_context():
            _seed_servers([_plex_entry("plex-a")])

            with patch(
                "media_preview_generator.web.routes.api_settings._reregister_plex_webhooks_after_secret_rotation",
            ) as mock_hook:
                response = client.post(
                    "/api/settings",
                    json={"webhook_secret": "brand-new-secret"},
                    headers={"X-Auth-Token": "test-token-12345678"},
                )

        assert response.status_code == 200, f"Settings save should succeed; got {response.status_code}"
        assert mock_hook.call_count == 1, (
            "Posting webhook_secret to /api/settings must trigger the re-register hook exactly once. "
            "Without this wiring, rotated secrets silently break Plex webhooks."
        )

        # Pin the settings instance handed to the hook actually carries the
        # NEW secret — otherwise a subtle ordering bug (hook called BEFORE
        # settings.update) would leave Plex re-registered with the old
        # token, defeating the whole rotation.
        call = mock_hook.call_args
        passed_settings = call.args[0] if call.args else call.kwargs.get("settings")
        assert passed_settings is not None, "re-register hook was called with no settings argument"
        assert passed_settings.get("webhook_secret") == "brand-new-secret", (
            f"re-register hook must see the freshly-saved secret; got {passed_settings.get('webhook_secret')!r}"
        )
