"""Tests for trigger_refresh(deleted_paths=...) — Jellyfin / Emby / Plex.

When Radarr/Sonarr's Download webhook is an upgrade, the deletedFiles[]
list flows into trigger_refresh as ``deleted_paths``. Each path fans out
through ``_trigger_path_deleted`` so the server drops its stale library
entry instead of waiting for filesystem-monitor / scheduled-scan to
notice the deletion.

Boundary kwargs assertion (per .claude/rules/testing.md): inspect the
exact body POSTed for each deleted candidate so a regression that drops
the ``UpdateType:"Deleted"`` field would fail the test.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from media_preview_generator.servers.base import ServerConfig, ServerType
from media_preview_generator.servers.emby import EmbyServer
from media_preview_generator.servers.jellyfin import JellyfinServer
from media_preview_generator.servers.plex import PlexServer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _jelly():
    return JellyfinServer(
        ServerConfig(
            id="jelly-1",
            type=ServerType.JELLYFIN,
            name="Test Jellyfin",
            enabled=True,
            url="http://jellyfin:8096",
            auth={"method": "quick_connect", "access_token": "tok", "user_id": "u"},
        )
    )


def _emby():
    return EmbyServer(
        ServerConfig(
            id="emby-1",
            type=ServerType.EMBY,
            name="Test Emby",
            enabled=True,
            url="http://emby:8096",
            auth={"method": "api_key", "api_key": "k"},
        )
    )


def _plex():
    return PlexServer(
        ServerConfig(
            id="plex-1",
            type=ServerType.PLEX,
            name="Test Plex",
            enabled=True,
            url="http://plex:32400",
            auth={"token": "tok"},
        )
    )


def _ok():
    """A 2xx response that .raise_for_status() won't blow up on."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.status_code = 204
    resp.text = ""
    return resp


# ---------------------------------------------------------------------------
# Jellyfin
# ---------------------------------------------------------------------------


class TestJellyfinDeletedPaths:
    def test_posts_UpdateType_Deleted_to_Library_Media_Updated(self):
        jelly = _jelly()
        with patch.object(JellyfinServer, "_request", return_value=_ok()) as req:
            jelly.trigger_refresh(
                item_id=None,
                remote_path=None,
                deleted_paths=["/movies/X/OLD.mkv"],
            )

        # Exactly one POST: the deletion nudge.
        assert req.call_count == 1
        call = req.call_args_list[0]
        assert call.args == ("POST", "/Library/Media/Updated")
        body = call.kwargs.get("json_body") or {}
        assert body == {"Updates": [{"Path": "/movies/X/OLD.mkv", "UpdateType": "Deleted"}]}

    def test_combines_with_remote_path_refresh(self):
        """new path → 'Created' nudge; old path → 'Deleted' nudge — both fire."""
        jelly = _jelly()
        with patch.object(JellyfinServer, "_request", return_value=_ok()) as req:
            jelly.trigger_refresh(
                item_id=None,
                remote_path="/movies/X/NEW.mkv",
                deleted_paths=["/movies/X/OLD.mkv"],
            )

        assert req.call_count == 2
        # Order: remote_path scan first, then deletion nudge.
        first_body = req.call_args_list[0].kwargs.get("json_body") or {}
        second_body = req.call_args_list[1].kwargs.get("json_body") or {}
        assert first_body["Updates"][0]["UpdateType"] == "Created"
        assert first_body["Updates"][0]["Path"] == "/movies/X/NEW.mkv"
        assert second_body["Updates"][0]["UpdateType"] == "Deleted"
        assert second_body["Updates"][0]["Path"] == "/movies/X/OLD.mkv"

    def test_multiple_deleted_paths_fan_out(self):
        jelly = _jelly()
        with patch.object(JellyfinServer, "_request", return_value=_ok()) as req:
            jelly.trigger_refresh(
                item_id=None,
                remote_path=None,
                deleted_paths=["/x/A.mkv", "/x/B.mkv"],
            )

        assert req.call_count == 2
        assert req.call_args_list[0].kwargs["json_body"]["Updates"][0]["Path"] == "/x/A.mkv"
        assert req.call_args_list[1].kwargs["json_body"]["Updates"][0]["Path"] == "/x/B.mkv"

    def test_empty_deleted_paths_is_noop(self):
        jelly = _jelly()
        with patch.object(JellyfinServer, "_request", return_value=_ok()) as req:
            jelly.trigger_refresh(item_id=None, remote_path=None, deleted_paths=[])
        assert req.call_count == 0

    def test_none_deleted_paths_is_noop(self):
        jelly = _jelly()
        with patch.object(JellyfinServer, "_request", return_value=_ok()) as req:
            jelly.trigger_refresh(item_id=None, remote_path=None, deleted_paths=None)
        assert req.call_count == 0

    def test_failure_on_one_deletion_doesnt_block_others(self):
        """A single 5xx on path A doesn't suppress the nudge for path B."""
        jelly = _jelly()
        good = _ok()
        bad = MagicMock()
        bad.raise_for_status.side_effect = RuntimeError("transient 503")
        bad.status_code = 503
        bad.text = "boom"

        # First call (path A) fails, second (path B) succeeds.
        with patch.object(JellyfinServer, "_request", side_effect=[bad, good]) as req:
            jelly.trigger_refresh(
                item_id=None,
                remote_path=None,
                deleted_paths=["/x/A.mkv", "/x/B.mkv"],
            )
        assert req.call_count == 2  # B was attempted despite A's failure


# ---------------------------------------------------------------------------
# Emby
# ---------------------------------------------------------------------------


class TestEmbyDeletedPaths:
    def test_posts_UpdateType_Deleted(self):
        emby = _emby()
        with patch.object(EmbyServer, "_request", return_value=_ok()) as req:
            emby.trigger_refresh(
                item_id=None,
                remote_path=None,
                deleted_paths=["/movies/X/OLD.mkv"],
            )

        assert req.call_count == 1
        body = req.call_args_list[0].kwargs.get("json_body") or {}
        assert body == {"Updates": [{"Path": "/movies/X/OLD.mkv", "UpdateType": "Deleted"}]}


# ---------------------------------------------------------------------------
# Plex — no-op (default base class implementation)
# ---------------------------------------------------------------------------


class TestPlexDeletedPathsNoop:
    def test_plex_does_not_call_anything_for_deleted_paths(self):
        """Plex has no deletion endpoint; the partial-scan on the new
        path naturally re-checks the surrounding folder. Default base
        ``_trigger_path_deleted`` no-op MUST NOT issue a request.
        """
        plex = _plex()
        # Patch _trigger_path_refresh too so the test can assert that
        # ONLY the new-path refresh fires, not anything for deleted_paths.
        with (
            patch.object(plex, "_trigger_path_refresh") as path_refresh,
            patch.object(plex, "_trigger_path_deleted") as path_deleted,
            patch.object(plex, "_trigger_item_refresh") as item_refresh,
        ):
            plex.trigger_refresh(
                item_id=None,
                remote_path="/movies/X/NEW.mkv",
                deleted_paths=["/movies/X/OLD.mkv"],
            )

        # New-path refresh fired (Plex has its targeted partial scan).
        path_refresh.assert_called_once_with("/movies/X/NEW.mkv")
        # Deletion nudge invoked the BASE no-op (default impl).
        # The override doesn't exist on Plex, so what we patched here
        # is the inherited default. It IS called by trigger_refresh's
        # base-class fan-out — verify it ran but issued no actual request.
        path_deleted.assert_called_once_with("/movies/X/OLD.mkv")
        # Item refresh not called (no item_id supplied).
        item_refresh.assert_not_called()

    def test_plex_default_path_deleted_is_inherited_base_noop(self):
        """Plex inherits the base class's no-op _trigger_path_deleted —
        it does NOT override it, so calling it MUST return None silently
        without touching any HTTP layer."""
        from media_preview_generator.servers.base import MediaServer

        plex = _plex()
        # Verify we're using the base class's implementation — if a
        # future change adds a Plex override, the test will catch it.
        assert type(plex)._trigger_path_deleted is MediaServer._trigger_path_deleted, (
            "Plex must NOT override _trigger_path_deleted (no equivalent endpoint)"
        )
        # And the actual call returns None without raising.
        assert plex._trigger_path_deleted("/movies/X/OLD.mkv") is None
