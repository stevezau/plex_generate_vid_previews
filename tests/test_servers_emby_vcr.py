"""Cassette-backed contract tests for ``EmbyServer`` vendor-API calls.

Pins the URL contract for Emby's exact-Path filter and Pass-1/Pass-2
fallbacks. Specifically catches:
- Audit L2 — ``IncludeItemTypes=Movie,Episode`` filter (a non-video
  item indexed at the same Path could otherwise return wrong id).
- The Path= filter URL shape and 200 OK + empty Items response on
  miss.

These initial cassettes are MISS-only — querying for a path that
doesn't exist. HIT cassettes can be added later against a dedicated
test library.
"""

from __future__ import annotations

import os

import pytest

from media_preview_generator.servers import EmbyServer, Library, ServerConfig, ServerType

pytestmark = [pytest.mark.vcr]

_MISS_PATH = "/data/Movies/MPG_Cassette_Sentinel_DoesNotExist_99999.mkv"


@pytest.fixture
def emby_under_test():
    url = os.environ.get("EMBY_URL", "http://fake-emby.local:8096")
    token = os.environ.get("EMBY_TOKEN", "fake-token")
    user_id = os.environ.get("EMBY_USER_ID", "")
    cfg = ServerConfig(
        id="emby-vcr",
        type=ServerType.EMBY,
        name="Emby VCR",
        enabled=True,
        url=url,
        auth={"method": "api_key", "api_key": token, "user_id": user_id}
        if user_id
        else {"method": "api_key", "api_key": token},
        verify_ssl=False,
        libraries=[
            # Provide a placeholder library so library-scoping doesn't
            # short-circuit before the API call is made. The cassette
            # records what Emby's exact-Path filter actually returns.
            Library(id="1", name="Movies", remote_paths=("/data/Movies",), enabled=True),
        ],
    )
    return EmbyServer(cfg)


class TestEmbyResolveOnePathContract:
    """Contract pins for the EmbyServer reverse-lookup chain.

    The MISS cassette captures Emby's ``GET /Items?Path=<exact>...``
    response when no item matches. Pins:
    - URL contains ``Path=<exact>`` AND
      ``IncludeItemTypes=Movie,Episode`` (audit L2).
    - ``X-Emby-Token`` header (scrubbed).
    - 200 OK with empty ``Items`` list.

    A regression that drops ``IncludeItemTypes`` would either
    cassette-miss (URL doesn't match) or re-record with a different
    URL shape — either way detection is loud.
    """

    def test_unknown_path_returns_none_via_path_filter(self, emby_under_test):
        item_id = emby_under_test._uncached_resolve_remote_path_to_item_id(_MISS_PATH)
        assert item_id is None
