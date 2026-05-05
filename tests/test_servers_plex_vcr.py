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

These initial cassettes are MISS-only — they query for a basename that
doesn't exist in any library. Captures the URL contract (``type=``,
``file=``) and the empty-result response shape without leaking any of
the user's library data into the committed cassette.

A future PR with a dedicated test library can add HIT cassettes
(querying for known-indexed test items) once the library is set up.

Recording workflow — see ``tests/cassettes/README.md``. Default
mode is replay-only; cassettes are committed to the repo and run
without a live Plex.
"""

from __future__ import annotations

import os

import pytest

from media_preview_generator.servers import PlexServer, ServerConfig, ServerType

pytestmark = [pytest.mark.vcr, pytest.mark.real_plex_server]

# A basename that's deliberately synthetic and identifying as a sentinel
# so a future maintainer knows the cassette is intentionally a MISS.
# Plex's ``file=`` filter does substring-on-Path matching; this name
# can't collide with any real media file.
_MISS_BASENAME = "MPG_Cassette_Sentinel_DoesNotExist_99999.mkv"


@pytest.fixture
def plex_server_under_test():
    """Construct a real :class:`PlexServer` for cassette recording / replay.

    During recording (``--record-mode=once``) the test hits the live
    server identified by ``PLEX_URL`` / ``PLEX_TOKEN`` env vars.
    During replay (default) those env vars are ignored — vcrpy
    intercepts the ``requests`` calls and serves the cassette body.
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

    Cassettes pin the EXACT URL Plex accepts: a regression that
    drops ``type=`` either gets a cassette miss in replay (URL
    doesn't match recorded shape) or, if re-recorded, would record
    a 500 instead of a 200 OK and the test assertion changes.
    """

    def test_unknown_path_returns_none_via_file_filter(self, plex_server_under_test):
        """A basename that doesn't exist in any library returns
        ``None`` — and the recorded request URL must contain
        ``type=<id>&file=<encoded basename>``.

        Cassette pins:
        - ``GET /library/sections/{key}/all?type=<id>&file=...`` for
          every video section (movie type=1, episode type=4).
        - ``X-Plex-Token`` header (scrubbed in the cassette).
        - 200 OK with empty MediaContainer.

        Resolver returns ``None`` because no item matches.
        """
        rating_key = plex_server_under_test._resolve_one_path(f"/data/Movies/{_MISS_BASENAME}")
        assert rating_key is None, (
            f"MISS cassette must yield None — got ratingKey={rating_key!r}. "
            "If this fails after re-recording, Plex started returning a result for the "
            "sentinel filename — pick a more obscure basename."
        )

    def test_empty_path_short_circuits_without_calling_plex(self, plex_server_under_test):
        """Empty input must not produce any HTTP calls. No cassette
        interaction is needed (the function returns None before
        touching the network). vcrpy in replay mode allows this
        because no requests are made.
        """
        assert plex_server_under_test._resolve_one_path("") is None
        assert plex_server_under_test._resolve_one_path(None) is None  # type: ignore[arg-type]
