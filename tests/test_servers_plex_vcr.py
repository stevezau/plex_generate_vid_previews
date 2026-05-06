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


# Synthetic test movie indexed by ``tests/integration/up.sh``. Path is
# fixed (not env-driven) so cassette replay doesn't depend on env vars.
_HIT_LIBRARY_REMOTE_PATH = "/media/Movies"
_HIT_BASENAME = "Test Movie H264 (2024).mkv"
_HIT_PATH = f"{_HIT_LIBRARY_REMOTE_PATH}/Test Movie H264 (2024)/{_HIT_BASENAME}"


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

    def test_known_path_resolves_to_rating_key(self, plex_server_under_test):
        """HIT cassette: a synthetic test movie indexed in the test stack
        resolves to a non-empty ratingKey via ``?type=&file=``.

        Pre-fix, the resolver dropped ``type=`` from the URL and Plex
        500'd silently → every Plex publish became
        SKIPPED_NOT_IN_LIBRARY. The mock unit test passed anyway
        because the mock returned items regardless of URL.

        Cassette pins the EXACT URL Plex accepts AND the response
        shape (MediaContainer with a Video child carrying ratingKey).
        Any drift on either side fails this test.
        """
        rating_key = plex_server_under_test._resolve_one_path(_HIT_PATH)
        assert rating_key, (
            f"HIT cassette must yield a ratingKey for {_HIT_PATH!r} — got {rating_key!r}. "
            "If this fails on a fresh recording, the test stack's Movies library is "
            "empty — re-run ./tests/integration/up.sh"
        )


class TestGetBundleMetadataContract:
    """Contract pin for ``PlexServer.get_bundle_metadata``.

    Production bug history (D31): ``get_bundle_metadata`` was called
    with the full ``/library/metadata/<id>`` path instead of the bare
    ratingKey, building ``/library/metadata//library/metadata/<id>/tree``
    which 404'd. Plex's response was misinterpreted as "not indexed".
    Every Plex publish from the canonical-path flow returned
    SKIPPED_NOT_INDEXED for hours of live traffic.

    This cassette pins:
    - The exact URL we send: ``/library/metadata/<bare>/tree``.
    - The XML response shape: ``<MediaContainer>`` with ``<Video>``
      → ``<Media>`` → ``<Part>`` carrying ``hash`` + ``file`` attrs.

    A regression that double-prefixes the URL gets a cassette miss on
    replay; a regression that drops the hash extraction fails the
    list-of-tuples assertion.
    """

    def test_bundle_metadata_returns_hash_and_path_for_known_item(self, plex_server_under_test):
        # Resolve the test movie's ratingKey first via the same path
        # the SUT uses; this proves end-to-end the rating-key → bundle
        # chain is wired correctly.
        rating_key = plex_server_under_test._resolve_one_path(_HIT_PATH)
        assert rating_key, "Pre-condition: test stack must have the movie indexed"

        parts = plex_server_under_test.get_bundle_metadata(rating_key)
        assert parts, (
            f"HIT cassette must return at least one (hash, path) tuple for ratingKey {rating_key!r}; "
            f"got {parts!r}. Plex's /tree endpoint must return MediaPart attrs."
        )
        bundle_hash, remote_path = parts[0]
        assert bundle_hash and len(bundle_hash) >= 8, f"bundle hash looks malformed: {bundle_hash!r}"
        assert remote_path.endswith(_HIT_BASENAME), (
            f"Expected MediaPart path to end with {_HIT_BASENAME!r}; got {remote_path!r}"
        )


class TestConnectionContract:
    """Contract pin for ``PlexServer.test_connection``.

    Probes ``GET /`` and parses the MediaContainer response for
    machineIdentifier, friendlyName, version. A regression that
    points at a different endpoint (e.g. ``/identity``, which Plex
    also serves but with a different shape) would silently lose
    the version + friendlyName fields. Cassette pins the URL +
    response shape simultaneously.
    """

    def test_connect_returns_identity_for_test_stack(self, plex_server_under_test):
        result = plex_server_under_test.test_connection()
        assert result.ok, f"connect failed: {result.message!r}"
        # ``server_id`` (machineIdentifier) is scrubbed to FAKE_PLEX_MID
        # in committed cassettes; assertion is on PRESENCE not value.
        assert result.server_id, "test_connection must surface server_id from MediaContainer"


class TestListLibrariesContract:
    """Contract pin for ``PlexServer.list_libraries``.

    Walks ``plex.library.sections`` (plexapi calls
    ``/library/sections``) and maps the Directory entries to our
    Library dataclass. A regression that drops the ``key`` or
    ``locations`` mapping shows up as missing libraries / wrong
    library ids in the dispatch — exactly the class of bug that
    causes "this file isn't owned by anyone" silent skips.
    """

    def test_lists_movies_library_from_test_stack(self, plex_server_under_test):
        libs = plex_server_under_test.list_libraries()
        assert libs, "test_stack must have at least one library"
        # The test stack mounts /media/Movies as a Plex Movies
        # section. Find it by name OR by the location prefix.
        movies = [
            lib for lib in libs if lib.name == "Movies" or any(p.startswith("/media/Movies") for p in lib.remote_paths)
        ]
        assert movies, f"expected a Movies library; got {[(lib.id, lib.name, lib.remote_paths) for lib in libs]!r}"
        assert movies[0].id, "library.id must be populated from Directory.key"
        assert movies[0].remote_paths, "library.remote_paths must be populated from Location entries"


class TestTriggerPathRefreshContract:
    """Contract pin for ``PlexServer._trigger_path_refresh``.

    Plex's targeted-scan endpoint accepts a folder path within a
    library section via ``GET /library/sections/{id}/refresh?path=``.
    A regression that misnames the param (e.g. ``folder=`` or
    ``directory=``) silently no-ops because Plex returns 200 OK
    regardless. Cassette pins the recorded URL so a param-rename
    regression fails on replay.
    """

    def test_partial_scan_uses_refresh_endpoint_with_path_param(self, plex_server_under_test):
        # A single _trigger_path_refresh call should fire one POST/GET
        # against /library/sections/<id>/refresh with the path
        # query-encoded. Cassette captures the exact request.
        # We pass the parent dir of the test movie (Plex's scan
        # endpoint takes a folder, not a file).
        plex_server_under_test._trigger_path_refresh(f"{_HIT_LIBRARY_REMOTE_PATH}/Test Movie H264 (2024)")
        # No assertion on return value (helper returns None) — the
        # cassette interaction itself is the assertion. A future
        # regression that renames the param mismatches the recorded
        # URL on replay.
