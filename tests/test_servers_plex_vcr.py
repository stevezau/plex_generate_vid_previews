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

NOTE: this module is the *infrastructure* — only the no-HTTP test
lives here today. Cassette-needing tests will land alongside their
recorded YAML files (one PR per cassette set) so the test code and
its replay data ship together. Per the user's directive, missing
cassettes must FAIL loudly, never skip — so we don't commit a test
without its cassette.
"""

from __future__ import annotations

import os

import pytest

from media_preview_generator.servers import PlexServer, ServerConfig, ServerType

# Every test in this module replays a recorded cassette. Default
# vcrpy record_mode='none' means "fail loudly if a cassette is
# missing" — that's the contract we want: tests without cassettes
# indicate a problem (the cassette was never recorded, or it was
# accidentally deleted), not a benign skip. Best practice: record
# cassettes BEFORE merging the test code, never the other way around.
pytestmark = [pytest.mark.vcr]


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


class TestResolveOnePathContract:
    """Contract pins for ``PlexServer._resolve_one_path``.

    The production bug this infrastructure is designed to catch:
    ``_resolve_one_path`` sent ``?file=<basename>`` without
    ``type=<media_type_id>``. Plex returned HTTP 500. Every Plex
    publish via this resolver was SKIPPED_NOT_IN_LIBRARY for hours
    of live traffic before detection. The mock-based unit test
    asserted ``"file=Foo.mkv" in ekey`` — passed because the mock
    returned items regardless of URL shape.

    Cassette-backed tests will pin the EXACT URL Plex accepts, so a
    future regression that drops ``type=`` (or breaks any other
    contract aspect) fails in replay because the cassette's recorded
    request doesn't match what the SUT now sends.
    """

    def test_empty_path_short_circuits_without_calling_plex(self, plex_server_under_test):
        """Empty input must not produce any HTTP calls. No cassette
        interaction is needed (the function returns None before
        touching the network). This is the only test in this module
        that runs without a recorded cassette — exercises the
        cheap-pre-check path.
        """
        assert plex_server_under_test._resolve_one_path("") is None
        assert plex_server_under_test._resolve_one_path(None) is None  # type: ignore[arg-type]
