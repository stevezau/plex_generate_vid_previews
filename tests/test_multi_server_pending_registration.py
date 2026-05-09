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


class TestAllFreshFastPathRegistrationRetry:
    """Regression: the all-fresh fast path in ``process_canonical_path``
    short-circuits when every publisher's outputs are already on disk
    and source-fresh — but it constructs ``PublisherResult`` rows
    DIRECTLY (not through ``_publish_one``). Pre-fix, every row was
    hardcoded to ``SKIPPED_OUTPUT_EXISTS``, so on the retry attempt of
    a PENDING_REGISTRATION dispatch the fast path would silently report
    "complete" while never firing the per-item registration calls.

    Reproduced live 2026-05-09: Bering Sea Gold S17E10 — Sonarr
    upgrade webhook fired, attempt #0 returned PENDING for both Emby
    and Jellyfin (item not yet indexed). Attempt #1 (30s later) hit
    the all-fresh fast path → all SKIPPED → "Retry chain complete on
    attempt #1" → trickplay never registered with Jellyfin until the
    3 AM scheduled scan.

    Fix: the fast path applies the same PENDING vs SKIPPED branching
    as ``_publish_one``'s skip-if-exists branch, fires
    ``trigger_refresh`` for registration-tier servers (so when
    item_id eventually resolves, the registration completes), and
    re-arms the retry queue when any row is PENDING.
    """

    def test_fast_path_returns_PENDING_for_jellyfin_with_unresolved_item_id(self, tmp_path, mock_config):
        """All-fresh fast path with Jellyfin + item_id=None → PENDING (not SKIPPED).

        End-to-end via process_canonical_path so the real fast-path
        code path is exercised.
        """
        from media_preview_generator.output.journal import write_meta
        from media_preview_generator.processing.multi_server import (
            MultiServerStatus,
            process_canonical_path,
        )
        from media_preview_generator.servers import ServerRegistry
        from media_preview_generator.servers.jellyfin import JellyfinServer

        media_dir = tmp_path / "Movie (2024)"
        media_dir.mkdir()
        live_mkv = media_dir / "Movie (2024) -REL.mkv"
        live_mkv.write_bytes(b"fake")
        # Pre-existing trickplay tile + journal so outputs_fresh_for_source returns True.
        sheet = media_dir / "Movie (2024) -REL.trickplay" / "320 - 10x10" / "0.jpg"
        sheet.parent.mkdir(parents=True)
        sheet.write_bytes(b"\xff\xd8\xff")
        write_meta([sheet], str(live_mkv), publisher="jellyfin_trickplay")

        registry = ServerRegistry.from_settings(
            [
                {
                    "id": "jelly-1",
                    "type": "jellyfin",
                    "name": "JellyTest",
                    "enabled": True,
                    "url": "http://jelly:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "libraries": [
                        {
                            "id": "1",
                            "name": "Movies",
                            "remote_paths": [str(tmp_path)],
                            "enabled": True,
                        }
                    ],
                    "exclude_paths": [],
                    "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 10},
                }
            ],
        )

        from unittest.mock import patch as _patch

        with (
            _patch.object(JellyfinServer, "trigger_refresh") as refresh_mock,
            _patch.object(JellyfinServer, "resolve_remote_path_to_item_id", return_value=None),
        ):
            mock_config.working_tmp_folder = str(tmp_path / "tmp")
            result = process_canonical_path(
                canonical_path=str(live_mkv),
                registry=registry,
                config=mock_config,
                schedule_retry_on_not_indexed=False,
            )

        # Aggregate status reflects PENDING (treated as published-shaped).
        assert result.status is MultiServerStatus.PUBLISHED
        # Per-publisher: PENDING, NOT SKIPPED_OUTPUT_EXISTS.
        assert len(result.publishers) == 1
        assert result.publishers[0].status is PublisherStatus.PUBLISHED_PENDING_REGISTRATION, (
            "Fast path with item_id=None on a registration-tier server MUST return "
            "PENDING_REGISTRATION so the retry queue re-arms — not SKIPPED_OUTPUT_EXISTS, "
            "which would silently mark the retry chain complete while the trickplay row "
            "never gets registered with Jellyfin."
        )
        # trigger_refresh fired so the registration call chain runs
        # (item_id may resolve on a future retry; this attempt was None).
        refresh_mock.assert_called_once()
        call = refresh_mock.call_args
        assert call.kwargs["item_id"] is None
        assert call.kwargs["remote_path"] == str(live_mkv)

    def test_fast_path_promotes_to_PUBLISHED_when_item_id_now_resolves(self, tmp_path, mock_config):
        """On a follow-up retry where item_id NOW resolves, fast path
        fires trigger_refresh with the resolved id (so the plugin-bridge
        + /Items/{id}/Refresh actually run) and returns SKIPPED_OUTPUT_EXISTS
        (no further retry needed)."""
        from media_preview_generator.output.journal import write_meta
        from media_preview_generator.processing.multi_server import (
            process_canonical_path,
        )
        from media_preview_generator.servers import ServerRegistry
        from media_preview_generator.servers.jellyfin import JellyfinServer

        media_dir = tmp_path / "Movie (2024)"
        media_dir.mkdir()
        live_mkv = media_dir / "Movie (2024).mkv"
        live_mkv.write_bytes(b"fake")
        sheet = media_dir / "Movie (2024).trickplay" / "320 - 10x10" / "0.jpg"
        sheet.parent.mkdir(parents=True)
        sheet.write_bytes(b"\xff\xd8\xff")
        write_meta([sheet], str(live_mkv), publisher="jellyfin_trickplay")

        registry = ServerRegistry.from_settings(
            [
                {
                    "id": "jelly-1",
                    "type": "jellyfin",
                    "name": "JellyTest",
                    "enabled": True,
                    "url": "http://jelly:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "libraries": [
                        {
                            "id": "1",
                            "name": "Movies",
                            "remote_paths": [str(tmp_path)],
                            "enabled": True,
                        }
                    ],
                    "exclude_paths": [],
                    "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 10},
                }
            ],
        )

        from unittest.mock import patch as _patch

        with (
            _patch.object(JellyfinServer, "trigger_refresh") as refresh_mock,
            _patch.object(
                JellyfinServer,
                "resolve_remote_path_to_item_id",
                return_value="resolved-item-id-42",
            ),
            # Pretend the plugin is installed so the resolver actually
            # calls resolve_remote_path_to_item_id (otherwise the
            # vendor-branching short-circuit returns None for Jellyfin
            # without plugin — see _make_item_id_resolver).
            _patch(
                "media_preview_generator.processing.multi_server._jellyfin_plugin_cached_installed",
                return_value=True,
            ),
        ):
            mock_config.working_tmp_folder = str(tmp_path / "tmp")
            result = process_canonical_path(
                canonical_path=str(live_mkv),
                registry=registry,
                config=mock_config,
                schedule_retry_on_not_indexed=False,
            )

        assert result.publishers[0].status is PublisherStatus.SKIPPED_OUTPUT_EXISTS
        # Critical: trigger_refresh fired WITH the resolved item_id so
        # the plugin-bridge + /Items/{id}/Refresh registration completes.
        refresh_mock.assert_called_once()
        call = refresh_mock.call_args
        assert call.kwargs["item_id"] == "resolved-item-id-42", (
            "Fast path on retry promotion MUST forward the resolved item_id to "
            "trigger_refresh — without this, the plugin-bridge / /Items/{id}/Refresh "
            "calls never fire and Jellyfin's library row stays un-registered."
        )

    def test_fast_path_plain_skip_for_plex_no_extra_calls(self, tmp_path, mock_config):
        """Plex doesn't use per-item registration — fast path should
        return plain SKIPPED_OUTPUT_EXISTS without firing trigger_refresh
        (saves an HTTP round-trip on every duplicate webhook for Plex)."""
        from media_preview_generator.output.journal import write_meta
        from media_preview_generator.processing.multi_server import process_canonical_path
        from media_preview_generator.servers import ServerRegistry
        from media_preview_generator.servers.plex import PlexServer

        # Build a Plex sidecar layout that mimics real Plex bundle BIF
        # so outputs_fresh_for_source returns True.
        plex_cfg = tmp_path / "plex_cfg"
        plex_cfg.mkdir()
        (plex_cfg / "Media" / "localhost").mkdir(parents=True)

        media_dir = tmp_path / "Movies"
        media_dir.mkdir()
        live_mkv = media_dir / "Movie.mkv"
        live_mkv.write_bytes(b"fake")

        # Plex's adapter needs server metadata, so we patch the resolution
        # to return an item id and patch compute_output_paths so the
        # bundle hash lookup doesn't need a real Plex.
        registry = ServerRegistry.from_settings(
            [
                {
                    "id": "plex-1",
                    "type": "plex",
                    "name": "PlexTest",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {"token": "tok"},
                    "libraries": [{"id": "1", "name": "Movies", "remote_paths": [str(media_dir)], "enabled": True}],
                    "exclude_paths": [],
                    "output": {"adapter": "plex_bundle", "plex_config_folder": str(plex_cfg)},
                }
            ],
        )

        # Fake Plex bundle BIF on disk.
        bundle_dir = plex_cfg / "Media" / "localhost" / "x" / "fakebundle.bundle" / "Contents" / "Indexes"
        bundle_dir.mkdir(parents=True)
        bif = bundle_dir / "index-sd.bif"
        bif.write_bytes(b"fake-bif")
        write_meta([bif], str(live_mkv), publisher="plex_bundle")

        from unittest.mock import patch as _patch

        from media_preview_generator.output.plex_bundle import PlexBundleAdapter

        with (
            _patch.object(PlexServer, "trigger_refresh") as refresh_mock,
            _patch.object(PlexServer, "resolve_remote_path_to_item_id", return_value="ratingKey-99"),
            _patch.object(PlexBundleAdapter, "compute_output_paths", return_value=[bif]),
        ):
            mock_config.working_tmp_folder = str(tmp_path / "tmp")
            result = process_canonical_path(
                canonical_path=str(live_mkv),
                registry=registry,
                config=mock_config,
                schedule_retry_on_not_indexed=False,
            )

        assert result.publishers[0].status is PublisherStatus.SKIPPED_OUTPUT_EXISTS
        # No trigger_refresh on Plex from the fast path (Plex isn't in
        # the registration tier; saving the HTTP call on duplicate webhooks).
        refresh_mock.assert_not_called()


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
