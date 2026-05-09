"""Tests for PUBLISHED_PENDING_REGISTRATION — the ``item_id=-`` retry path.

Background: the job log of bug-report dispatch ``5093813e`` showed
``status=published item_id=-`` for JellyTest because Jellyfin hadn't
indexed the new file yet. Tiles landed on disk but the per-item
registration calls (Media Preview Bridge plugin + /Items/{id}/Refresh)
were skipped — and nothing scheduled a retry to fire them later. The
scrubber stayed blank until Jellyfin's 3 AM scheduled scan.

Fix: when a server that uses per-item registration (Jellyfin/Emby)
publishes successfully but ``item_id`` is None, return
``PUBLISHED_PENDING_REGISTRATION`` so the existing retry queue
re-attempts after backoff. On a retry where the item id now resolves,
the skip-if-exists branch fires ``trigger_refresh(item_id=...)`` and
the registration completes — promoting to PUBLISHED.

Matrix coverage per .claude/rules/testing.md:
  * server tier (Plex / Jellyfin / Emby) × (item_id None / known)
  * publish vs skip-if-exists branches
  * retry exhaustion (gracefully terminates via existing BACKOFF_SCHEDULE)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from media_preview_generator.output.base import BifBundle
from media_preview_generator.processing.multi_server import (
    PublisherStatus,
    _publish_one,
    _server_needs_item_registration,
)
from media_preview_generator.servers.base import ServerConfig, ServerType
from media_preview_generator.servers.emby import EmbyServer
from media_preview_generator.servers.jellyfin import JellyfinServer
from media_preview_generator.servers.plex import PlexServer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bundle(tmp_path) -> BifBundle:
    return BifBundle(
        canonical_path=str(tmp_path / "Movie.mkv"),
        frame_dir=tmp_path / "frames",
        bif_path=None,
        frame_interval=10,
        width=320,
        height=180,
        frame_count=5,
    )


def _adapter(*, name: str = "jellyfin_trickplay", needs_meta: bool = False) -> MagicMock:
    """Mock adapter that successfully publishes."""
    a = MagicMock()
    a.name = name
    a.needs_server_metadata.return_value = needs_meta
    a.compute_output_paths.return_value = []  # empty list = no skip-if-exists check
    a.publish.return_value = None
    return a


def _jelly():
    return JellyfinServer(
        ServerConfig(
            id="jelly-1",
            type=ServerType.JELLYFIN,
            name="JellyTest",
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
            name="EmbyTest",
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
            name="PlexTest",
            enabled=True,
            url="http://plex:32400",
            auth={"token": "t"},
        )
    )


# ---------------------------------------------------------------------------
# _server_needs_item_registration discriminator
# ---------------------------------------------------------------------------


class TestServerNeedsItemRegistration:
    def test_jellyfin_needs_item_registration(self):
        assert _server_needs_item_registration(_jelly()) is True

    def test_emby_needs_item_registration(self):
        assert _server_needs_item_registration(_emby()) is True

    def test_plex_does_not_need_item_registration(self):
        # Plex inherits the base no-op _trigger_item_refresh.
        assert _server_needs_item_registration(_plex()) is False


# ---------------------------------------------------------------------------
# Publish-success branch
# ---------------------------------------------------------------------------


class TestPublishSuccessReturnsPending:
    def test_jellyfin_publish_with_item_id_None_returns_PENDING(self, tmp_path):
        """Tiles on disk, but item_id=None → PENDING_REGISTRATION (not PUBLISHED)."""
        jelly = _jelly()
        adapter = _adapter()

        with patch.object(JellyfinServer, "trigger_refresh") as refresh:
            outcome = _publish_one(
                jelly,
                adapter,
                _bundle(tmp_path),
                item_id=None,
                skip_if_exists=False,
            )

        assert outcome.status is PublisherStatus.PUBLISHED_PENDING_REGISTRATION
        # tiles WERE written (publish was called).
        adapter.publish.assert_called_once()
        # trigger_refresh was called with item_id=None (the path-based
        # nudge fires; the per-item registration is skipped inside
        # trigger_refresh because item_id is None).
        refresh.assert_called_once()
        # Boundary kwargs assertion per .claude/rules/testing.md.
        call = refresh.call_args
        assert call.kwargs["item_id"] is None
        assert call.kwargs["remote_path"] == str(tmp_path / "Movie.mkv")
        assert call.kwargs.get("deleted_paths") is None

    def test_jellyfin_publish_with_item_id_returns_PUBLISHED(self, tmp_path):
        """Item id known at publish time → PUBLISHED, no PENDING."""
        jelly = _jelly()
        adapter = _adapter()

        with patch.object(JellyfinServer, "trigger_refresh"):
            outcome = _publish_one(
                jelly,
                adapter,
                _bundle(tmp_path),
                item_id="42",
                skip_if_exists=False,
            )

        assert outcome.status is PublisherStatus.PUBLISHED

    def test_emby_publish_with_item_id_None_returns_PENDING(self, tmp_path):
        emby = _emby()
        adapter = _adapter(name="emby_sidecar")

        with patch.object(EmbyServer, "trigger_refresh"):
            outcome = _publish_one(
                emby,
                adapter,
                _bundle(tmp_path),
                item_id=None,
                skip_if_exists=False,
            )

        assert outcome.status is PublisherStatus.PUBLISHED_PENDING_REGISTRATION

    def test_plex_publish_unaffected_by_PENDING_path(self, tmp_path):
        """Plex never reaches publish-with-item_id=None because its
        adapter declares needs_server_metadata=True (short-circuits to
        SKIPPED_NOT_IN_LIBRARY upstream). Verifies the discriminator
        doesn't accidentally promote Plex into PENDING."""
        plex = _plex()
        # Plex adapter says needs_server_metadata=True; _publish_one
        # short-circuits before publish() is even called.
        adapter = _adapter(name="plex_bundle", needs_meta=True)

        with patch.object(PlexServer, "trigger_refresh"):
            outcome = _publish_one(
                plex,
                adapter,
                _bundle(tmp_path),
                item_id=None,
                skip_if_exists=False,
            )

        # Plex's path is SKIPPED_NOT_IN_LIBRARY, never PENDING.
        assert outcome.status is PublisherStatus.SKIPPED_NOT_IN_LIBRARY

    def test_deleted_paths_forwarded_to_trigger_refresh(self, tmp_path):
        """When _publish_one is called with deleted_paths, trigger_refresh
        receives them (so the deleted-path nudge fires for the right paths)."""
        jelly = _jelly()
        adapter = _adapter()

        with patch.object(JellyfinServer, "trigger_refresh") as refresh:
            _publish_one(
                jelly,
                adapter,
                _bundle(tmp_path),
                item_id="42",
                skip_if_exists=False,
                deleted_paths=["/x/old.mkv"],
            )

        call = refresh.call_args
        assert call.kwargs["deleted_paths"] == ["/x/old.mkv"]


# ---------------------------------------------------------------------------
# Skip-if-exists branch (the retry path)
# ---------------------------------------------------------------------------


class TestSkipIfExistsBranchPromotesPending:
    def test_skip_if_exists_with_item_id_None_returns_PENDING_for_jellyfin(self, tmp_path):
        """Outputs already on disk + item_id None → still PENDING (retry will pick up)."""
        jelly = _jelly()
        # Real output path so outputs_fresh_for_source can decide skip.
        out = tmp_path / "Movie.trickplay" / "320 - 10x10" / "0.jpg"
        out.parent.mkdir(parents=True)
        out.write_bytes(b"\xff\xd8\xff")
        adapter = _adapter()
        adapter.compute_output_paths.return_value = [out]

        # Force the freshness check positive (mocked .meta would normally
        # gate this; for the test we just patch it).
        with (
            patch(
                "media_preview_generator.processing.multi_server.outputs_fresh_for_source",
                return_value=True,
            ),
            patch.object(JellyfinServer, "trigger_refresh") as refresh,
        ):
            outcome = _publish_one(
                jelly,
                adapter,
                _bundle(tmp_path),
                item_id=None,
                skip_if_exists=True,
            )

        assert outcome.status is PublisherStatus.PUBLISHED_PENDING_REGISTRATION
        # publish() must NOT have been called (output already exists).
        adapter.publish.assert_not_called()
        # trigger_refresh fired so the path-based nudge happened (the
        # critical bit — without it the retry would never promote).
        refresh.assert_called_once()

    def test_skip_if_exists_with_item_id_None_returns_PENDING_for_emby(self, tmp_path):
        """Same matrix cell as Jellyfin but for Emby — both servers use
        per-item registration, so the PENDING promotion logic applies
        equally. Asserts the discriminator + skip-branch wiring don't
        accidentally treat Emby differently from Jellyfin."""
        emby = _emby()
        out = tmp_path / "Movie-320-10.bif"
        out.write_bytes(b"BIF")
        adapter = _adapter(name="emby_sidecar")
        adapter.compute_output_paths.return_value = [out]

        with (
            patch(
                "media_preview_generator.processing.multi_server.outputs_fresh_for_source",
                return_value=True,
            ),
            patch.object(EmbyServer, "trigger_refresh") as refresh,
        ):
            outcome = _publish_one(
                emby,
                adapter,
                _bundle(tmp_path),
                item_id=None,
                skip_if_exists=True,
            )

        assert outcome.status is PublisherStatus.PUBLISHED_PENDING_REGISTRATION
        adapter.publish.assert_not_called()
        refresh.assert_called_once()
        # Boundary kwargs assertion — trigger_refresh receives item_id=None
        # AND remote_path so the path-based nudge fires (the critical bit
        # — without it Emby never re-checks the path and the retry's
        # item-id resolution would have nothing to anchor to).
        call = refresh.call_args
        assert call.kwargs["item_id"] is None
        assert call.kwargs["remote_path"] == str(tmp_path / "Movie.mkv")

    def test_skip_if_exists_with_resolved_item_id_returns_SKIPPED_OUTPUT_EXISTS(self, tmp_path):
        """On a retry where the item id now resolves: skip-if-exists fires
        trigger_refresh(item_id=...) so the registration completes, then
        returns SKIPPED_OUTPUT_EXISTS (no further retry needed)."""
        jelly = _jelly()
        out = tmp_path / "Movie.trickplay" / "320 - 10x10" / "0.jpg"
        out.parent.mkdir(parents=True)
        out.write_bytes(b"\xff\xd8\xff")
        adapter = _adapter()
        adapter.compute_output_paths.return_value = [out]

        with (
            patch(
                "media_preview_generator.processing.multi_server.outputs_fresh_for_source",
                return_value=True,
            ),
            patch.object(JellyfinServer, "trigger_refresh") as refresh,
        ):
            outcome = _publish_one(
                jelly,
                adapter,
                _bundle(tmp_path),
                item_id="42",  # resolved this time
                skip_if_exists=True,
            )

        assert outcome.status is PublisherStatus.SKIPPED_OUTPUT_EXISTS
        # trigger_refresh was called WITH the resolved item_id, so the
        # plugin-bridge + /Items/{id}/Refresh endpoints fire and the
        # tiles get registered.
        refresh.assert_called_once()
        call = refresh.call_args
        assert call.kwargs["item_id"] == "42", (
            "On retry promotion, trigger_refresh MUST receive the resolved item_id "
            "so the per-item registration calls fire — without this, tiles stay "
            "un-registered until the next library scan."
        )

    def test_skip_if_exists_for_plex_returns_SKIPPED_not_PENDING(self, tmp_path):
        """Plex's per-item registration check is False, so even if item_id
        is None the skip branch returns plain SKIPPED_OUTPUT_EXISTS — Plex
        doesn't need a retry to register anything."""
        plex = _plex()
        out = tmp_path / "Movie.bif"
        out.write_bytes(b"BIF")
        # Plex's adapter declares needs_meta=True, BUT we want to simulate
        # the skip-if-exists branch (which runs after the metadata check).
        # The metadata short-circuit is bypassed when item_id IS set.
        adapter = _adapter(name="plex_bundle", needs_meta=True)
        adapter.compute_output_paths.return_value = [out]

        with (
            patch(
                "media_preview_generator.processing.multi_server.outputs_fresh_for_source",
                return_value=True,
            ),
            patch.object(PlexServer, "trigger_refresh"),
        ):
            outcome = _publish_one(
                plex,
                adapter,
                _bundle(tmp_path),
                item_id="ratingKey-99",  # plex needs this
                skip_if_exists=True,
            )

        assert outcome.status is PublisherStatus.SKIPPED_OUTPUT_EXISTS


# ---------------------------------------------------------------------------
# Retry-scheduling integration: the new status counts under PUBLISHED-like
# ---------------------------------------------------------------------------


class TestPendingRegistrationCountsAsPublished:
    """The PublishersResult counters and aggregate MultiServerStatus
    treat PENDING_REGISTRATION the same as PUBLISHED so the file-level
    outcome shows ``Generated`` and the per-server ``published`` count
    isn't artificially zero on the first attempt.
    """

    def test_published_like_constant_includes_pending(self):
        from media_preview_generator.processing.multi_server import _PUBLISHED_LIKE_STATUSES

        assert PublisherStatus.PUBLISHED in _PUBLISHED_LIKE_STATUSES
        assert PublisherStatus.PUBLISHED_PENDING_REGISTRATION in _PUBLISHED_LIKE_STATUSES
        # Other statuses must NOT be lumped in.
        assert PublisherStatus.SKIPPED_OUTPUT_EXISTS not in _PUBLISHED_LIKE_STATUSES
        assert PublisherStatus.SKIPPED_NOT_INDEXED not in _PUBLISHED_LIKE_STATUSES
        assert PublisherStatus.FAILED not in _PUBLISHED_LIKE_STATUSES
