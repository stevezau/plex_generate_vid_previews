"""Cassette-backed contract tests for ``JellyfinServer`` vendor-API calls.

Pins the URL contract for:
- The Media Preview Bridge plugin's ``ResolvePath`` lookup.
- The plugin's ``Trickplay/{itemId}`` registration endpoint
  (audit L1 — params must include ``width`` + ``intervalMs``
  matching the trickplay adapter's configured values).

These initial cassettes are MISS-only — querying for a path that
doesn't exist + a fake item_id for the trickplay registration. HIT
cassettes can be added later against a dedicated test library.
"""

from __future__ import annotations

import os

import pytest

from media_preview_generator.servers import JellyfinServer, Library, ServerConfig, ServerType

pytestmark = [pytest.mark.vcr]

_MISS_PATH = "/data/Movies/MPG_Cassette_Sentinel_DoesNotExist_99999.mkv"
# Synthetic test movie indexed by ``tests/integration/up.sh``.
_HIT_LIBRARY_REMOTE_PATH = "/jf-media/Movies"
_HIT_PATH = f"{_HIT_LIBRARY_REMOTE_PATH}/Test Movie H264 (2024)/Test Movie H264 (2024).mkv"


@pytest.fixture
def jellyfin_under_test():
    url = os.environ.get("JELLYFIN_URL", "http://fake-jellyfin.local:8096")
    token = os.environ.get("JELLYFIN_TOKEN", "fake-token")
    user_id = os.environ.get("JELLYFIN_USER_ID", "")
    auth = {"method": "api_key", "api_key": token}
    if user_id:
        auth["user_id"] = user_id
    cfg = ServerConfig(
        id="jellyfin-vcr",
        type=ServerType.JELLYFIN,
        name="Jellyfin VCR",
        enabled=True,
        url=url,
        auth=auth,
        verify_ssl=False,
        libraries=[
            Library(id="1", name="Movies", remote_paths=("/data/Movies",), enabled=True),
        ],
        # Use non-default output settings so the L1 contract test
        # asserts the params reflect adapter config, not hardcoded
        # defaults.
        output={"adapter": "jellyfin_trickplay", "width": 480, "frame_interval": 5},
    )
    return JellyfinServer(cfg)


class TestJellyfinResolveOnePathContract:
    """Pins the plugin's ``ResolvePath`` URL contract via a MISS
    cassette. A regression that drops the plugin call (or sends to
    a different endpoint) cassette-misses on replay.
    """

    def test_unknown_path_returns_none(self, jellyfin_under_test):
        item_id = jellyfin_under_test._uncached_resolve_remote_path_to_item_id(_MISS_PATH)
        assert item_id is None


class TestJellyfinTrickplayRegistrationContract:
    """Audit L1 contract pin: ``trigger_refresh`` calls the plugin's
    ``POST /MediaPreviewBridge/Trickplay/{itemId}`` with ``width`` and
    ``intervalMs`` query params reflecting the trickplay adapter's
    configured values. The cassette records the EXACT request shape
    the plugin accepts.
    """

    def test_plugin_call_uses_adapter_width_and_interval(self, jellyfin_under_test):
        # Use a fake item_id; plugin returns 404 (no such item) but
        # the cassette captures the URL shape.
        jellyfin_under_test._trigger_item_refresh("MPG_FAKE_ITEM_ID_99999")


class TestJellyfinConnectionContract:
    """Contract pin for ``JellyfinServer.test_connection``."""

    def test_connect_returns_identity_for_test_stack(self, jellyfin_under_test):
        result = jellyfin_under_test.test_connection()
        assert result.ok, f"connect failed: {result.message!r}"
        assert result.server_id, "test_connection must surface server_id from /System/Info"


class TestJellyfinListLibrariesContract:
    """Contract pin for ``JellyfinServer.list_libraries``."""

    def test_lists_test_stack_movies_library(self, jellyfin_under_test):
        libs = jellyfin_under_test.list_libraries()
        assert libs, "test_stack must have at least one library"
        movies = [lib for lib in libs if any(p.startswith("/jf-media/Movies") for p in lib.remote_paths)]
        assert movies, (
            f"expected a Movies library at /jf-media/Movies; got {[(lib.id, lib.name, lib.remote_paths) for lib in libs]!r}"
        )
        assert movies[0].id


class TestJellyfinTriggerPathRefreshContract:
    """Contract pin for ``JellyfinServer._trigger_path_refresh``.

    Jellyfin's path-based scan-nudge is ``POST /Library/Media/Updated``
    with body
    ``{"Updates":[{"Path":"…","UpdateType":"Created"}]}``.
    Note: Jellyfin uses ``"Created"`` while Emby uses ``"Modified"``
    — this divergence exists because Jellyfin's library monitor
    semantics treat the path-nudge as a "new file appeared" event.
    A regression that drops to Emby's ``"Modified"`` value still
    works (Jellyfin accepts both) but the cassette pins the
    documented contract.

    A regression that misnames the body field would return 204
    silently from Jellyfin, leaving the scan unnudged. Cassette is
    the only safety net.
    """

    def test_path_refresh_posts_library_media_updated(self, jellyfin_under_test):
        jellyfin_under_test._trigger_path_refresh(_HIT_PATH)
        # No return value to assert on — the cassette interaction IS
        # the assertion. A future regression that changes the URL or
        # body shape gets a cassette miss on replay.
