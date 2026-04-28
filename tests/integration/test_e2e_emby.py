"""End-to-end integration tests against a live Emby container.

Verifies the multi-server stack against a real Emby Server (started by
``docker-compose.test.yml``):

* :class:`EmbyServer` connects, authenticates, lists libraries, and
  resolves item ids → paths against the live HTTP API.
* :class:`ServerRegistry.find_owning_servers` correctly identifies
  which configured server owns a given canonical path after applying
  per-server path mappings.
* :func:`process_canonical_path` runs FFmpeg once and publishes a
  Emby-flavoured sidecar BIF next to the source media.
* The frame cache prevents a second FFmpeg pass for a duplicate dispatch.

Run with::

    pytest -m integration --no-cov tests/integration/test_e2e_emby.py
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from plex_generate_previews.processing.frame_cache import get_frame_cache
from plex_generate_previews.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from plex_generate_previews.servers import ServerRegistry


def _populate_synthetic_frames(directory: Path, count: int) -> None:
    """Write decodable JPGs that the Emby BIF packer accepts."""
    from PIL import Image

    directory.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (320, 180), (10, 20, 30))
    for i in range(count):
        img.save(directory / f"{i:05d}.jpg", "JPEG", quality=70)


def _media_servers_payload(emby_credentials: dict[str, str], canonical_root: str) -> list[dict]:
    """Build a media_servers settings entry for the live Emby container."""
    return [
        {
            "id": "emby-int-1",
            "type": "emby",
            "name": "Test Emby",
            "enabled": True,
            "url": emby_credentials["EMBY_URL"],
            "auth": {
                "method": "password",
                "access_token": emby_credentials["EMBY_ACCESS_TOKEN"],
                "user_id": emby_credentials["EMBY_USER_ID"],
            },
            "server_identity": emby_credentials["EMBY_SERVER_ID"],
            "libraries": [
                {
                    "id": "movies",
                    "name": "Movies",
                    "remote_paths": ["/em-media/Movies"],
                    "enabled": True,
                }
            ],
            "path_mappings": [
                {"remote_prefix": "/em-media", "local_prefix": canonical_root},
            ],
            "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 10},
        }
    ]


@pytest.fixture
def live_registry(emby_credentials, media_root):
    """A :class:`ServerRegistry` wired up to the live Emby container.

    The local mount point matches the docker-compose volume mount
    (``./media`` on the host → ``/em-media`` in the Emby container).
    The path mapping translates them into the same canonical path.
    """
    raw_servers = _media_servers_payload(emby_credentials, str(media_root))
    return ServerRegistry.from_settings(raw_servers, legacy_config=None)


@pytest.mark.integration
class TestEmbyConnection:
    def test_test_connection_succeeds(self, live_registry):
        """Live Emby server identifies itself via /System/Info."""
        server = live_registry.get("emby-int-1")
        result = server.test_connection()
        assert result.ok, result.message
        assert result.server_id  # populated from Emby's response

    def test_list_libraries_returns_movies(self, live_registry):
        """The Movies library we configured in setup_servers.py is enumerable."""
        server = live_registry.get("emby-int-1")
        libraries = server.list_libraries()
        assert any(lib.name == "Movies" for lib in libraries), [lib.name for lib in libraries]

    def test_list_items_returns_test_movies(self, live_registry):
        """Emby has scanned the synthetic test fixtures."""
        server = live_registry.get("emby-int-1")
        libraries = server.list_libraries()
        movies_lib = next((lib for lib in libraries if lib.name == "Movies"), None)
        assert movies_lib is not None

        items = list(server.list_items(movies_lib.id))
        # generate_test_media.sh produces 2 movies in /em-media/Movies; TV
        # shows are in a different folder so they don't appear here.
        names = [item.title for item in items]
        assert len(items) >= 1, f"expected scanned movies, got: {names}"

    def test_resolve_item_to_remote_path(self, live_registry):
        """Item-id → server-side path lookup works against the live API."""
        server = live_registry.get("emby-int-1")
        libraries = server.list_libraries()
        movies_lib = next(lib for lib in libraries if lib.name == "Movies")
        item = next(iter(server.list_items(movies_lib.id)))

        resolved = server.resolve_item_to_remote_path(item.id)
        assert resolved == item.remote_path


@pytest.mark.integration
class TestEmbyOwnershipResolution:
    def test_canonical_path_matches_emby_library(self, live_registry, media_root):
        """find_owning_servers translates remote_paths through path_mappings."""
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        matches = live_registry.find_owning_servers(canonical)

        assert len(matches) == 1
        match = matches[0]
        assert match.server_id == "emby-int-1"
        assert match.library_name == "Movies"

    def test_path_outside_library_has_no_owners(self, live_registry, tmp_path):
        """A path under no enabled library matches no server (the
        permanent-skip case)."""
        canonical = str(tmp_path / "not-in-library.mkv")
        assert live_registry.find_owning_servers(canonical) == []


@pytest.mark.integration
class TestEmbyEndToEndPublish:
    """Full pipeline: extract frames (mocked) → publish sidecar BIF → verify."""

    def test_publishes_sidecar_bif_next_to_media(self, live_registry, media_root, mock_config, tmp_path):
        """process_canonical_path lands an Emby sidecar BIF on disk
        next to the source media file the live Emby container saw."""
        # Use the H264 fixture (the docker volume mounts it as
        # /em-media/Movies/Test Movie H264 (2024)/...).
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")

        mock_config.working_tmp_folder = str(tmp_path / "work")
        mock_config.plex_bif_frame_interval = 10
        Path(mock_config.working_tmp_folder).mkdir(parents=True, exist_ok=True)

        # Synthetic frames: skip FFmpeg (saves a second of test runtime
        # and keeps the assertion strict — the BIF that lands is purely
        # a function of these inputs).
        def _fake_generate_images(media, tmp_path_, *_a, **_kw):
            _populate_synthetic_frames(Path(tmp_path_), count=5)
            return ("h264", 5, "120fps", 320)

        with patch(
            "plex_generate_previews.processing.multi_server.generate_images",
            side_effect=_fake_generate_images,
        ):
            result = process_canonical_path(
                canonical_path=canonical,
                registry=live_registry,
                config=mock_config,
            )

        assert result.status is MultiServerStatus.PUBLISHED, result.message
        assert result.published_count == 1

        # Sidecar exists at the documented location.
        bundle_dir = Path(canonical).parent
        sidecar = bundle_dir / "Test Movie H264 (2024)-320-10.bif"
        try:
            assert sidecar.exists()
            # And the publisher result references it.
            published = next(p for p in result.publishers if p.status is PublisherStatus.PUBLISHED)
            assert any(str(sidecar) == str(p) for p in published.output_paths)
        finally:
            # Clean up so a re-run of the test starts from a clean state
            # (the read-only docker mount means Emby never pushes its
            # own; this BIF was written by us).
            if sidecar.exists():
                sidecar.unlink()

    def test_frame_cache_prevents_second_ffmpeg_pass(self, live_registry, media_root, mock_config, tmp_path):
        """A second dispatch for the same canonical path hits the cache."""
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")

        mock_config.working_tmp_folder = str(tmp_path / "work")
        mock_config.plex_bif_frame_interval = 10
        Path(mock_config.working_tmp_folder).mkdir(parents=True, exist_ok=True)

        bundle_dir = Path(canonical).parent
        sidecar = bundle_dir / "Test Movie H264 (2024)-320-10.bif"
        if sidecar.exists():
            sidecar.unlink()

        try:
            ffmpeg_calls = 0

            def _counting_generate_images(media, tmp_path_, *_a, **_kw):
                nonlocal ffmpeg_calls
                ffmpeg_calls += 1
                _populate_synthetic_frames(Path(tmp_path_), count=5)
                return ("h264", 5, "120fps", 320)

            with patch(
                "plex_generate_previews.processing.multi_server.generate_images",
                side_effect=_counting_generate_images,
            ):
                # First dispatch: cache miss → FFmpeg runs → publish.
                first = process_canonical_path(
                    canonical_path=canonical,
                    registry=live_registry,
                    config=mock_config,
                )
                assert first.status is MultiServerStatus.PUBLISHED

                # Force regenerate=True so the publisher re-runs (otherwise
                # it short-circuits via skip-if-exists). The cache check
                # happens BEFORE the publisher, so cache hit still saves
                # FFmpeg even on regenerate.
                cache = get_frame_cache(base_dir=str(Path(mock_config.working_tmp_folder) / "frame_cache"))
                assert cache.get(canonical) is not None  # populated

                # Second dispatch: cache hit → no FFmpeg → re-publishes.
                # Emby sidecar already exists, so PublisherStatus is
                # SKIPPED_OUTPUT_EXISTS (regenerate=False default).
                second = process_canonical_path(
                    canonical_path=canonical,
                    registry=live_registry,
                    config=mock_config,
                )
                assert second.status in (
                    MultiServerStatus.SKIPPED,
                    MultiServerStatus.PUBLISHED,
                )

            # FFmpeg was invoked exactly once across both dispatches.
            assert ffmpeg_calls == 1, f"expected 1 FFmpeg call, got {ffmpeg_calls}"
        finally:
            if sidecar.exists():
                sidecar.unlink()


@pytest.mark.integration
class TestEmbyServerIdentityForWebhookRouting:
    def test_captured_server_identity_matches_live_emby(self, emby_credentials, live_registry):
        """The server_identity we persisted at setup matches /System/Info."""
        server = live_registry.get("emby-int-1")
        result = server.test_connection()
        assert result.ok
        # The identity captured during setup_servers.py must match the
        # one the live server reports right now — that's the contract
        # the webhook router relies on for multi-server-of-same-vendor
        # routing.
        assert result.server_id == emby_credentials["EMBY_SERVER_ID"]


@pytest.mark.integration
class TestEmbyTriggerRefresh:
    def test_trigger_refresh_does_not_raise(self, live_registry, media_root):
        """The post-publish refresh nudge succeeds against the live server."""
        server = live_registry.get("emby-int-1")
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        # Should not raise; failures are best-effort and swallowed.
        server.trigger_refresh(item_id=None, remote_path=canonical)
