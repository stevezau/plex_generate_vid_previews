"""Cassette-backed contract tests for ``PlexServer`` vendor-API calls.

These tests replay recorded HTTP interactions against Plex's API so the
exact request shape (URL, query params, headers) and response shape
(status code, JSON structure) is verified — no mocking. Catches the
class of bug that the mock-based unit tests miss: vendor API contract
drift that's invisible when the mock returns canned data regardless of
what the SUT sent.

Specifically pins the production bug that shipped to live: every Plex
publish was SKIPPED_NOT_IN_LIBRARY because ``_resolve_one_path``
constructed the URL without the required ``type=<media_type_id>``
parameter, and Plex returned HTTP 500. The mock unit test asserted
``"file=Foo.mkv" in ekey`` and missed it. With a cassette, the recorded
request shape (correct ``?type=4&file=...``) is the single source of
truth for what Plex requires.

Recording workflow — see ``tests/cassettes/README.md``. Default
mode is replay-only; cassettes are committed to the repo and run
without a live Plex.
"""

from __future__ import annotations

import os

import pytest

from media_preview_generator.servers import PlexServer, ServerConfig, ServerType

# Mark every test in this module so the suite-wide markers / addopts
# don't accidentally exclude them.
pytestmark = [pytest.mark.vcr]


# ---------------------------------------------------------------------------
# Fixtures: build a PlexServer pointing at the live server when recording,
# at a fake URL when replaying. The cassette absorbs the difference.
# ---------------------------------------------------------------------------


@pytest.fixture
def plex_server_under_test():
    """Construct a real :class:`PlexServer` for cassette recording / replay.

    During recording (``--record-mode=once``) the test hits the live
    server identified by ``PLEX_URL`` / ``PLEX_TOKEN`` environment
    variables. During replay (default) those env vars are ignored —
    vcrpy intercepts the ``requests`` calls and serves the cassette
    body. The fake URL still has to look syntactically valid so
    plexapi's URL builder doesn't reject it before vcr can intercept.
    """
    url = os.environ.get("PLEX_URL", "https://fake-plex.local:32400")
    token = os.environ.get("PLEX_TOKEN", "fake-token")
    cfg = ServerConfig(
        id="plex-vcr",
        type=ServerType.PLEX,
        name="Plex VCR",
        enabled=True,
        url=url,
        auth={"token": token, "method": "token"},
        verify_ssl=False,
        libraries=[],
        path_mappings=[],
    )
    return PlexServer(cfg)


# ---------------------------------------------------------------------------
# _resolve_one_path — the bug that motivated this test infrastructure
# ---------------------------------------------------------------------------


class TestResolveOnePathContract:
    """The production bug: ``_resolve_one_path`` sent
    ``?file=<basename>`` without ``type=<media_type_id>``. Plex
    returned HTTP 500. Every Plex publish via this resolver was
    SKIPPED_NOT_IN_LIBRARY for ~3 hours of live traffic before
    detection. The mock-based unit test asserted
    ``"file=Foo.mkv" in ekey`` — passed because the mock returned items
    regardless of URL shape.

    Cassettes pin the EXACT URL Plex accepts, so a future regression
    that drops ``type=`` (or breaks any other contract aspect) fails
    in replay because the cassette's recorded request doesn't match
    what the SUT now sends.
    """

    def test_episode_lookup_returns_rating_key(self, plex_server_under_test):
        """A known-indexed episode should resolve to its ratingKey via
        the file= filter against the TV library section. The cassette
        records the exact request shape Plex needs (type=4 for
        episodes, URL-encoded basename, X-Plex-Token header).
        """
        # NOTE: at recording time the path must point at a real episode
        # in the live Plex's TV library. After capture, the cassette's
        # response_body locks in a specific ratingKey — replay will
        # always return that same key regardless of what's in Plex now.
        # If Plex's stored path changes, re-record.
        path = os.environ.get(
            "PLEX_VCR_TV_PATH",
            "/data_16tb/TV Shows/Boy Band Confidential (2026) [imdb-tt41046343]/Season 01/"
            "Boy Band Confidential (2026) - S01E01 - The Price of Pop "
            "[AMZN WEBDL-1080p][EAC3 2.0][h264].mkv",
        )

        rating_key = plex_server_under_test._resolve_one_path(path)

        # Replay assertion: the recorded ratingKey must come back. If
        # Plex changes its API contract (drops the file= filter,
        # changes the response shape, requires a different parameter)
        # and we re-record, the cassette captures the new shape and
        # this test still passes WITH the corrected code; but old code
        # against the new cassette will fail because the URL no longer
        # matches — exactly the contract-drift detection the
        # cassette pattern is designed for.
        assert rating_key is not None, (
            "Cassette replay returned None for a path that was recorded as found. "
            "Either the cassette doesn't match the request the SUT sent (a contract "
            "regression — check the URL shape, headers, query params), or the "
            "cassette is stale and needs re-recording against a live Plex."
        )
        assert rating_key.isdigit(), (
            f"Plex ratingKey must be a numeric string; got {rating_key!r}. "
            "If this fails, the resolver is returning the URL form (e.g. "
            "'/library/metadata/12345') instead of the bare key — D31 regression."
        )

    def test_unknown_path_returns_none_not_500(self, plex_server_under_test):
        """A path that doesn't exist in any Plex section must return
        None — NOT raise, NOT return a stale ratingKey. Cassette
        records Plex's actual response for a missing file (the file=
        filter returns an empty MediaContainer with size=0).

        This case is critical because the production bug fired here
        too: when the URL was malformed (missing type=), Plex returned
        500 instead of "no match". The cassette captures the correct
        behaviour for the success path; a regression that re-introduces
        the malformed URL gets a cassette miss in replay.
        """
        rating_key = plex_server_under_test._resolve_one_path(
            "/data_16tb/TV Shows/Definitely Not A Real Show/S99E99.mkv"
        )
        assert rating_key is None, (
            f"Unknown path must return None, got ratingKey={rating_key!r}. "
            "A non-None return indicates basename collision detection (the "
            "endswith(target_tail) check) is misfiring."
        )

    def test_empty_path_short_circuits_without_calling_plex(self, plex_server_under_test):
        """Empty input must not produce any HTTP calls. No cassette
        interaction is recorded (the function returns None before
        touching the network). vcrpy in replay mode will allow this
        because no requests are made.
        """
        assert plex_server_under_test._resolve_one_path("") is None
        assert plex_server_under_test._resolve_one_path(None) is None  # type: ignore[arg-type]
