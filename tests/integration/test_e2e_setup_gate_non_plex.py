"""Setup gate accepts non-Plex installs end-to-end against live containers.

The user's scenario: I run only Jellyfin (or only Emby), I don't have
Plex at all, the setup wizard should NOT trap me on the setup page.

Before commit 7ebe59f, ``SettingsManager.is_configured()`` only returned
True if ``plex_url + plex_token`` were set. Now it accepts any
well-formed enabled entry in ``media_servers[]``. This test exercises
the full path: load real Jellyfin / Emby credentials into a fresh
``SettingsManager`` instance, confirm ``is_configured()`` and
``is_setup_complete()`` return True.
"""

from __future__ import annotations

import pytest

from media_preview_generator.web.settings_manager import SettingsManager


def _jellyfin_entry(jellyfin_credentials: dict[str, str]) -> dict:
    return {
        "id": "jf-int-1",
        "type": "jellyfin",
        "name": "Test Jellyfin",
        "enabled": True,
        "url": jellyfin_credentials["JELLYFIN_URL"],
        "auth": {"method": "api_key", "api_key": jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"]},
        "server_identity": jellyfin_credentials["JELLYFIN_SERVER_ID"],
        "libraries": [{"id": "movies", "name": "Movies", "remote_paths": ["/jf-media/Movies"], "enabled": True}],
    }


def _emby_entry(emby_credentials: dict[str, str]) -> dict:
    return {
        "id": "emby-int-1",
        "type": "emby",
        "name": "Test Emby",
        "enabled": True,
        "url": emby_credentials["EMBY_URL"],
        "auth": {
            "method": "password",
            "access_token": emby_credentials["EMBY_ACCESS_TOKEN"],
            "user_id": emby_credentials["EMBY_USER_ID"],
        },
        "server_identity": emby_credentials["EMBY_SERVER_ID"],
        "libraries": [{"id": "movies", "name": "Movies", "remote_paths": ["/em-media/Movies"], "enabled": True}],
    }


@pytest.mark.integration
class TestSetupGateNonPlex:
    """B1: setup gate accepts Emby- or Jellyfin-only installs."""

    def test_jellyfin_only_install_passes_setup_gate(self, jellyfin_credentials, tmp_path):
        """Configuring ONLY Jellyfin (no Plex) should mark setup as complete."""
        sm = SettingsManager(config_dir=str(tmp_path))
        sm.set("media_servers", [_jellyfin_entry(jellyfin_credentials)])

        assert sm.is_configured(), (
            "is_configured() should accept a Jellyfin-only install — the user shouldn't be trapped on /setup."
        )
        assert sm.is_setup_complete(), "is_setup_complete() should follow is_configured() for non-Plex installs."

    def test_emby_only_install_passes_setup_gate(self, emby_credentials, tmp_path):
        """Same for Emby-only."""
        sm = SettingsManager(config_dir=str(tmp_path))
        sm.set("media_servers", [_emby_entry(emby_credentials)])

        assert sm.is_configured()
        assert sm.is_setup_complete()

    def test_jellyfin_plus_emby_install_passes_setup_gate(self, jellyfin_credentials, emby_credentials, tmp_path):
        """No Plex but multiple non-Plex servers — still configured."""
        sm = SettingsManager(config_dir=str(tmp_path))
        sm.set(
            "media_servers",
            [_jellyfin_entry(jellyfin_credentials), _emby_entry(emby_credentials)],
        )

        assert sm.is_configured()
        assert sm.is_setup_complete()

    def test_disabled_jellyfin_does_not_count(self, jellyfin_credentials, tmp_path):
        """An entry with enabled=False should NOT satisfy the gate."""
        sm = SettingsManager(config_dir=str(tmp_path))
        entry = _jellyfin_entry(jellyfin_credentials)
        entry["enabled"] = False
        sm.set("media_servers", [entry])

        assert not sm.is_configured(), "disabled entries shouldn't satisfy the setup gate"

    def test_jellyfin_missing_api_key_does_not_count(self, jellyfin_credentials, tmp_path):
        """Well-formed shape but missing credentials → still not configured."""
        sm = SettingsManager(config_dir=str(tmp_path))
        entry = _jellyfin_entry(jellyfin_credentials)
        entry["auth"] = {"method": "api_key", "api_key": ""}
        sm.set("media_servers", [entry])

        assert not sm.is_configured(), "an entry without an api_key shouldn't pass the gate"

    def test_emby_password_flow_access_token_passes_gate(self, emby_credentials, tmp_path):
        """Emby's password-method auth uses ``access_token`` (not ``api_key``).

        Both auth flows yield usable credentials; the gate must accept
        either. Regression for the bug found while building this test:
        previously is_configured() only checked ``api_key``, so password-
        flow Emby installs were stuck on the setup page.
        """
        sm = SettingsManager(config_dir=str(tmp_path))
        # Use the password flow shape directly (no api_key, just access_token + user_id).
        entry = _emby_entry(emby_credentials)
        assert entry["auth"]["method"] == "password"
        assert entry["auth"]["access_token"]  # captured by setup_servers.py
        sm.set("media_servers", [entry])

        assert sm.is_configured()

    def test_empty_media_servers_fails_gate(self, tmp_path):
        """No servers at all → setup is incomplete."""
        sm = SettingsManager(config_dir=str(tmp_path))
        sm.set("media_servers", [])

        assert not sm.is_configured()
        assert not sm.is_setup_complete()
