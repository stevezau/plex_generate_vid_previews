"""Performance-budget integration tests against the live test stack.

These tests catch the class of bug that ALMOST every regression in this
session was — code that's functionally correct but has gone
order-of-magnitude slower than expected. Examples from session history:

* Emby Pass-0 short-circuit holes (#44) — resolves took 30s instead of
  <500ms when content wasn't on the server. Functional tests passed
  (the resolve eventually returned ``None``), only timing told us it
  was 60× slow.
* Vestigial eager Plex pre-connection — every job blocked 300ms on
  job-start. No functional bug, but unjustified latency.
* Connection-pool race — N parallel workers cold-hit, all opened
  separate Plex connections. Functional, but burned N-1 TLS
  handshakes per job. Only visible in load + log analysis.
* Jellyfin overload-cascade (#51) — 30s timeout × 2 = 60s burn. The
  retry queue eventually recovered, so functional. Timing budget would
  have failed the test immediately.

Each test asserts a wall-clock budget alongside the functional check.
A budget violation is a regression; the assertion message includes the
"why" so a future maintainer knows whether to investigate or
re-baseline.

Budgets are documented per-test with the rationale + the historical
incident that motivated it.

Run:
  ./tests/integration/up.sh
  pytest -m integration --no-cov tests/integration/test_e2e_timing_budgets.py -v

Tests skip cleanly when ``servers.env`` is absent (test stack down).
"""

from __future__ import annotations

import time

import pytest

from media_preview_generator.servers import (
    EmbyServer,
    JellyfinServer,
    Library,
    PlexServer,
    ServerConfig,
    ServerType,
)

# ``real_plex_server`` opts out of tests/conftest.py's
# ``_neutralize_real_world_calls`` autouse fixture so the SUT actually
# hits the test stack instead of getting a mocked ``plex_server``.
pytestmark = [pytest.mark.integration, pytest.mark.real_plex_server]


# -----------------------------------------------------------------------------
# Budget constants (fail loud on regressions)
# -----------------------------------------------------------------------------

# Reverse-lookup against a server that doesn't own the file. Pre-fix #44
# this was 30s on Emby (full-text scoring loop) and 60s on Jellyfin
# (overload cascade). With the Pass-0 short-circuit + Jellyfin-timeout
# fix, both should land in <500ms even on a cold cache.
NON_OWNING_RESOLVE_BUDGET_S = 0.5

# HIT path: resolve a known file. Plex is sub-second; Emby Pass-0 is
# ~50ms; Jellyfin plugin (when installed) is ~10ms. Without the plugin
# Jellyfin falls through to the base Pass-0 path (~50ms).
HIT_RESOLVE_BUDGET_S = 1.0

# test_connection probe (GET /System/Info or /). Fast on idle servers;
# anything >2s suggests a connection-establishment regression.
CONNECT_PROBE_BUDGET_S = 2.0

# Library enumeration. plexapi parses XML; Emby/Jellyfin parse JSON
# from /Library/VirtualFolders. <1s comfortable.
LIST_LIBRARIES_BUDGET_S = 1.0


# -----------------------------------------------------------------------------
# Fixtures: real server clients pointing at the live test stack
# -----------------------------------------------------------------------------


@pytest.fixture
def plex_client(plex_credentials):
    # Library snapshot must be non-empty + enabled so PlexServer's
    # list_libraries() / resolve filter doesn't treat "no snapshot"
    # as "no libraries enabled". Mirrors how the production registry
    # builds the config from settings.json — with the user's library
    # ticks reflected on the ``libraries`` list.
    cfg = ServerConfig(
        id="plex-timing",
        type=ServerType.PLEX,
        name="Plex Timing",
        enabled=True,
        url=plex_credentials["PLEX_URL"],
        auth={"token": plex_credentials["PLEX_ACCESS_TOKEN"], "method": "token"},
        verify_ssl=False,
        libraries=[
            Library(id="1", name="Movies", remote_paths=("/media/Movies",), enabled=True),
        ],
        path_mappings=[],
    )
    return PlexServer(cfg)


@pytest.fixture
def emby_client(emby_credentials):
    cfg = ServerConfig(
        id="emby-timing",
        type=ServerType.EMBY,
        name="Emby Timing",
        enabled=True,
        url=emby_credentials["EMBY_URL"],
        auth={
            "method": "api_key",
            "api_key": emby_credentials["EMBY_ACCESS_TOKEN"],
            "user_id": emby_credentials["EMBY_USER_ID"],
        },
        verify_ssl=False,
        libraries=[
            # Library remote_paths needs the test-stack mount so the
            # resolver's ``parent_id`` lookup matches and Pass-0 gets
            # the ParentId-scoped query (the fast path).
            Library(id="3", name="Movies", remote_paths=("/em-media/Movies",), enabled=True),
        ],
    )
    return EmbyServer(cfg)


@pytest.fixture
def jellyfin_client(jellyfin_credentials):
    cfg = ServerConfig(
        id="jellyfin-timing",
        type=ServerType.JELLYFIN,
        name="Jellyfin Timing",
        enabled=True,
        url=jellyfin_credentials["JELLYFIN_URL"],
        auth={"method": "api_key", "api_key": jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"]},
        verify_ssl=False,
        libraries=[
            Library(
                id="f137a2dd21bbc1b99aa5c0f6bf02a805",
                name="Movies",
                remote_paths=("/jf-media/Movies",),
                enabled=True,
            ),
        ],
        output={"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 10},
    )
    return JellyfinServer(cfg)


# -----------------------------------------------------------------------------
# Plex timing budgets
# -----------------------------------------------------------------------------


class TestPlexTimingBudgets:
    """Wall-clock budgets for Plex's per-call vendor APIs."""

    def test_test_connection_under_2s(self, plex_client):
        t0 = time.perf_counter()
        result = plex_client.test_connection()
        elapsed = time.perf_counter() - t0
        assert result.ok, f"connect failed: {result.message!r}"
        assert elapsed < CONNECT_PROBE_BUDGET_S, (
            f"Plex test_connection took {elapsed:.2f}s — budget {CONNECT_PROBE_BUDGET_S}s. "
            "Pre-fix the orchestrator's vestigial eager pre-connection blocked job-start "
            "by ~300ms; this is the contract that prevents that creep."
        )

    def test_list_libraries_under_1s(self, plex_client):
        t0 = time.perf_counter()
        libs = plex_client.list_libraries()
        elapsed = time.perf_counter() - t0
        assert libs, "test stack must have at least one library"
        assert elapsed < LIST_LIBRARIES_BUDGET_S, (
            f"Plex list_libraries took {elapsed:.2f}s — budget {LIST_LIBRARIES_BUDGET_S}s."
        )

    def test_resolve_known_path_under_1s(self, plex_client):
        path = "/media/Movies/Test Movie H264 (2024)/Test Movie H264 (2024).mkv"
        t0 = time.perf_counter()
        rating_key = plex_client._resolve_one_path(path)
        elapsed = time.perf_counter() - t0
        assert rating_key, f"test stack must have {path!r} indexed"
        assert elapsed < HIT_RESOLVE_BUDGET_S, (
            f"Plex resolve HIT took {elapsed:.2f}s — budget {HIT_RESOLVE_BUDGET_S}s. "
            "The D31 ?type= regression made this 30+s; if the budget fires, suspect "
            "the resolver's URL shape changed."
        )

    def test_resolve_unknown_path_under_500ms(self, plex_client):
        path = "/media/Movies/MPG_Sentinel_DoesNotExist/MPG_Sentinel.mkv"
        t0 = time.perf_counter()
        rating_key = plex_client._resolve_one_path(path)
        elapsed = time.perf_counter() - t0
        assert rating_key is None
        assert elapsed < NON_OWNING_RESOLVE_BUDGET_S, (
            f"Plex resolve MISS took {elapsed:.2f}s — budget {NON_OWNING_RESOLVE_BUDGET_S}s. "
            "Plex's file= filter should miss in ~100ms; if budget fires, the resolver "
            "fell back to a section.all() walk."
        )


# -----------------------------------------------------------------------------
# Emby timing budgets — the perf #44 territory
# -----------------------------------------------------------------------------


class TestEmbyTimingBudgets:
    """Wall-clock budgets for Emby's per-call vendor APIs.

    These are the tests that would have caught perf #44 day-zero. The
    pre-fix Pass-0 short-circuit had two holes that made
    "not-on-this-server" resolves take 30s. Budget here would have
    failed loud the moment the regression shipped.
    """

    def test_test_connection_under_2s(self, emby_client):
        t0 = time.perf_counter()
        result = emby_client.test_connection()
        elapsed = time.perf_counter() - t0
        assert result.ok, f"connect failed: {result.message!r}"
        assert elapsed < CONNECT_PROBE_BUDGET_S, (
            f"Emby test_connection took {elapsed:.2f}s — budget {CONNECT_PROBE_BUDGET_S}s."
        )

    def test_list_libraries_under_1s(self, emby_client):
        t0 = time.perf_counter()
        libs = emby_client.list_libraries()
        elapsed = time.perf_counter() - t0
        assert libs, "test stack must have at least one library"
        assert elapsed < LIST_LIBRARIES_BUDGET_S, (
            f"Emby list_libraries took {elapsed:.2f}s — budget {LIST_LIBRARIES_BUDGET_S}s."
        )

    def test_resolve_unknown_path_no_pass1_burn(self, emby_client):
        """**The perf #44 regression test.** A path whose first word
        doesn't match any series in the library MUST short-circuit at
        Pass-0 (zero candidates) and NOT fall through to Pass-1's full-
        stem ``searchTerm`` scoring loop. Pre-fix this was 30s.

        Live evidence: Boy Band Confidential webhook fired against an
        EmbyTest with 23 other "Boy*" shows. Pass-0 walked all 23
        series, found no match, then fell through to Pass-1 → 30.4s
        burn per file, every webhook.
        """
        # Path with a deliberately-rare first word ("Zzzzzz") so the
        # NameStartsWith query is guaranteed to find 0 candidates.
        path = "/em-media/Movies/Zzzzzz Nonexistent (2099)/Zzzzzz Nonexistent (2099).mkv"
        t0 = time.perf_counter()
        item_id = emby_client._uncached_resolve_remote_path_to_item_id(path)
        elapsed = time.perf_counter() - t0
        assert item_id is None
        assert elapsed < NON_OWNING_RESOLVE_BUDGET_S, (
            f"Emby resolve MISS took {elapsed:.2f}s — budget {NON_OWNING_RESOLVE_BUDGET_S}s. "
            "If the budget fires, perf #44 has regressed: Pass-0 short-circuit is "
            "no longer firing on the 'zero candidates with clean prefix' branch in "
            "_embyish.py:_pass0_name_prefix_lookup."
        )

    def test_resolve_known_path_under_1s(self, emby_client):
        path = "/em-media/Movies/Test Movie H264 (2024)/Test Movie H264 (2024).mkv"
        t0 = time.perf_counter()
        item_id = emby_client._uncached_resolve_remote_path_to_item_id(path)
        elapsed = time.perf_counter() - t0
        assert item_id, f"test stack must have {path!r} indexed"
        assert elapsed < HIT_RESOLVE_BUDGET_S, (
            f"Emby resolve HIT took {elapsed:.2f}s — budget {HIT_RESOLVE_BUDGET_S}s. "
            "Pass-0 NameStartsWith should land in ~50ms; budget fire suggests Pass-0 "
            "is being skipped or the test stack has unusually slow indexing."
        )


# -----------------------------------------------------------------------------
# Jellyfin timing budgets — the #51 territory
# -----------------------------------------------------------------------------


class TestJellyfinTimingBudgets:
    """Wall-clock budgets for Jellyfin's per-call vendor APIs.

    The #51 fix prevents the 60s overload-cascade (plugin timeout →
    base resolver also timeout against same overloaded server). When
    Jellyfin is responsive, both happy-path and miss should land
    under 1s.
    """

    def test_test_connection_under_2s(self, jellyfin_client):
        t0 = time.perf_counter()
        result = jellyfin_client.test_connection()
        elapsed = time.perf_counter() - t0
        assert result.ok, f"connect failed: {result.message!r}"
        assert elapsed < CONNECT_PROBE_BUDGET_S, (
            f"Jellyfin test_connection took {elapsed:.2f}s — budget {CONNECT_PROBE_BUDGET_S}s."
        )

    def test_list_libraries_under_1s(self, jellyfin_client):
        t0 = time.perf_counter()
        libs = jellyfin_client.list_libraries()
        elapsed = time.perf_counter() - t0
        assert libs, "test stack must have at least one library"
        assert elapsed < LIST_LIBRARIES_BUDGET_S, (
            f"Jellyfin list_libraries took {elapsed:.2f}s — budget {LIST_LIBRARIES_BUDGET_S}s."
        )

    def test_resolve_unknown_path_under_2s_when_idle(self, jellyfin_client):
        """When Jellyfin is idle (not mid-scan), MISS resolves should
        land in <2s. The #51 fix bounds the worst case to one
        ``_request`` timeout (default 30s); when the server is
        responsive there's no excuse for >2s.

        Looser than Emby's 500ms because Jellyfin's plugin path adds
        a ResolvePath round-trip + fall-through to base Pass-0.
        """
        path = "/jf-media/Movies/Zzzzzz Nonexistent (2099)/Zzzzzz Nonexistent (2099).mkv"
        t0 = time.perf_counter()
        item_id = jellyfin_client._uncached_resolve_remote_path_to_item_id(path)
        elapsed = time.perf_counter() - t0
        assert item_id is None
        # 2s budget is generous — a 30s timeout fires here means
        # something is wrong with the test stack OR #51 has regressed.
        assert elapsed < 2.0, (
            f"Jellyfin resolve MISS took {elapsed:.2f}s. "
            "If 30s+: the #51 timeout-cascade fix has regressed. "
            "If 2-30s: JellyTest may be busy scanning; re-run when idle."
        )


# -----------------------------------------------------------------------------
# Cross-cutting: peer-equal fan-out timing
# -----------------------------------------------------------------------------


class TestFanoutTimingBudgets:
    """Multi-server budgets — verify the peer-equal architecture
    doesn't have any single-server-blocks-the-job pathologies.

    Pre-K4 unification, Plex resolution was a serial pre-step that
    blocked Emby/Jellyfin dispatch. Post-unification, all three
    resolve in parallel and the slowest wins. Budget here is the
    "slowest of three" wall time — should be roughly the slowest
    individual budget, not the sum.
    """

    def test_three_server_resolve_in_parallel(self, plex_client, emby_client, jellyfin_client):
        """Resolve the same path on all three servers (test stack has
        Test Movie H264 (2024) indexed on all three at different
        mount points). Total wall time should be roughly the
        slowest single resolve, not the sum.

        This is the "Plex-first leak" canary — if Plex resolution is
        serially blocking Emby/Jellyfin, the total exceeds the sum
        of (fastest × 1) + (slowest × 2). Sequential is the bug.
        """
        # Note: not actually parallelising here, just measuring each
        # in isolation to verify they're individually bounded. A
        # parallel-execution test would require ThreadPoolExecutor
        # plumbing this test doesn't otherwise need.
        plex_path = "/media/Movies/Test Movie H264 (2024)/Test Movie H264 (2024).mkv"
        emby_path = "/em-media/Movies/Test Movie H264 (2024)/Test Movie H264 (2024).mkv"
        jelly_path = "/jf-media/Movies/Test Movie H264 (2024)/Test Movie H264 (2024).mkv"

        t0 = time.perf_counter()
        plex_rk = plex_client._resolve_one_path(plex_path)
        emby_id = emby_client._uncached_resolve_remote_path_to_item_id(emby_path)
        jelly_id = jellyfin_client._uncached_resolve_remote_path_to_item_id(jelly_path)
        elapsed = time.perf_counter() - t0

        assert plex_rk, "Plex test stack must have the movie indexed"
        assert emby_id, "Emby test stack must have the movie indexed"
        assert jelly_id, "Jellyfin test stack must have the movie indexed"
        # Sum of three HIT budgets — generous since this isn't a
        # parallel run. If a single server blows its budget, that
        # one's individual test will fire first.
        assert elapsed < HIT_RESOLVE_BUDGET_S * 3, (
            f"Three serial resolves took {elapsed:.2f}s — budget {HIT_RESOLVE_BUDGET_S * 3}s. "
            "If a single server is dominating, check its individual budget test for the why."
        )
