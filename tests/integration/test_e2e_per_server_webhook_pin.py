"""B2: ``/api/webhooks/server/<id>`` pins dispatch to the named server.

End-to-end against live Plex + Jellyfin. Both servers own the same path.
Posting to the per-server webhook URL must publish to ONLY the named
server, even though the universal URL would publish to both.

Verifies the production behaviour the B2 fix added in commit 7ebe59f:
``server_id_filter`` flows from the URL through ``_dispatch_canonical_path``
into ``process_canonical_path`` and there filters the publishers list.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from media_preview_generator.processing import multi_server as ms_module
from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from media_preview_generator.servers import ServerRegistry


def _legacy_config(plex_credentials, tmp_path) -> MagicMock:
    config = MagicMock()
    config.plex_url = plex_credentials["PLEX_URL"]
    config.plex_token = plex_credentials["PLEX_ACCESS_TOKEN"]
    config.plex_timeout = 60
    config.plex_libraries = ["Movies"]
    config.plex_config_folder = str(tmp_path / "plex_config")
    Path(config.plex_config_folder).mkdir(parents=True, exist_ok=True)
    config.plex_local_videos_path_mapping = ""
    config.plex_videos_path_mapping = ""
    config.path_mappings = []
    config.plex_bif_frame_interval = 5
    config.thumbnail_quality = 4
    config.regenerate_thumbnails = False
    config.gpu_threads = 0
    config.cpu_threads = 2
    config.gpu_config = []
    config.tmp_folder = str(tmp_path / "tmp")
    config.working_tmp_folder = str(tmp_path / "tmp")
    Path(config.working_tmp_folder).mkdir(parents=True, exist_ok=True)
    config.tmp_folder_created_by_us = False
    config.ffmpeg_path = "/usr/bin/ffmpeg"
    config.ffmpeg_threads = 2
    config.tonemap_algorithm = "hable"
    config.log_level = "INFO"
    config.worker_pool_timeout = 60
    config.plex_library_ids = None
    config.plex_verify_ssl = True
    return config


def _two_server_registry(plex_credentials, jellyfin_credentials, legacy_config, media_root):
    """Plex + Jellyfin sharing one Movies library."""
    raw_servers = [
        {
            "id": "plex-int-1",
            "type": "plex",
            "name": "Test Plex",
            "enabled": True,
            "url": plex_credentials["PLEX_URL"],
            "auth": {"method": "token", "token": plex_credentials["PLEX_ACCESS_TOKEN"]},
            "server_identity": plex_credentials["PLEX_SERVER_ID"],
            "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/media/Movies"], "enabled": True}],
            "path_mappings": [{"remote_prefix": "/media", "local_prefix": str(media_root)}],
            "output": {
                "adapter": "plex_bundle",
                "plex_config_folder": legacy_config.plex_config_folder,
                "frame_interval": 5,
            },
        },
        {
            "id": "jf-int-1",
            "type": "jellyfin",
            "name": "Test Jellyfin",
            "enabled": True,
            "url": jellyfin_credentials["JELLYFIN_URL"],
            "auth": {"method": "api_key", "api_key": jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"]},
            "server_identity": jellyfin_credentials["JELLYFIN_SERVER_ID"],
            "libraries": [{"id": "movies", "name": "Movies", "remote_paths": ["/jf-media/Movies"], "enabled": True}],
            "path_mappings": [{"remote_prefix": "/jf-media", "local_prefix": str(media_root)}],
            "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 5},
        },
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=legacy_config)


def _cleanup_outputs(canonical: str, plex_config_folder: str | None) -> None:
    parent = Path(canonical).parent
    base = Path(canonical).stem
    sidecar = parent / f"{base}-320-5.bif"
    if sidecar.exists():
        sidecar.unlink()
    trickplay = parent / "trickplay"
    if trickplay.exists():
        shutil.rmtree(trickplay)
    if plex_config_folder and Path(plex_config_folder).exists():
        media_dir = Path(plex_config_folder) / "Metadata"
        if media_dir.exists():
            shutil.rmtree(media_dir)


@pytest.fixture(autouse=True)
def _isolate_frame_cache():
    from media_preview_generator.processing.frame_cache import reset_frame_cache

    reset_frame_cache()
    yield
    reset_frame_cache()


@pytest.mark.integration
@pytest.mark.real_plex_server
@pytest.mark.slow
class TestPerServerUrlPinsDispatch:
    """B2: ``/api/webhooks/server/<id>`` scopes dispatch to one server only."""

    def test_per_server_dispatch_skips_sibling_publishers(
        self,
        plex_credentials,
        jellyfin_credentials,
        media_root,
        tmp_path,
    ):
        """Plex+Jellyfin both own the path; per-server URL fires Plex only.

        Without ``server_id_filter`` both publishers would fire — surprising
        given the URL says "this webhook is for *this* server".
        """
        legacy_config = _legacy_config(plex_credentials, tmp_path)
        registry = _two_server_registry(plex_credentials, jellyfin_credentials, legacy_config, media_root)

        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        _cleanup_outputs(canonical, legacy_config.plex_config_folder)

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=legacy_config,
                server_id_filter="plex-int-1",
            )

            assert result.status is MultiServerStatus.PUBLISHED, result.message
            # ONLY Plex should have published.
            assert len(result.publishers) == 1, (
                f"Expected exactly 1 publisher with server_id_filter='plex-int-1'; "
                f"got {len(result.publishers)}: {[p.adapter_name for p in result.publishers]}"
            )
            plex_pub = result.publishers[0]
            assert plex_pub.adapter_name == "plex_bundle"
            assert plex_pub.server_id == "plex-int-1"
            assert plex_pub.status is PublisherStatus.PUBLISHED

            # Jellyfin trickplay artifacts must NOT exist.
            trickplay_dir = Path(canonical).parent / "trickplay"
            assert not trickplay_dir.exists(), (
                f"Per-server URL pinned to Plex created Jellyfin trickplay at {trickplay_dir} — "
                "the server_id_filter wasn't respected."
            )
        finally:
            _cleanup_outputs(canonical, legacy_config.plex_config_folder)

    def test_universal_url_fires_both_publishers(
        self,
        plex_credentials,
        jellyfin_credentials,
        media_root,
        tmp_path,
    ):
        """Sanity: WITHOUT the filter, both publishers fire — i.e. the
        per-server pinning above is a real behaviour change, not the
        default."""
        legacy_config = _legacy_config(plex_credentials, tmp_path)
        registry = _two_server_registry(plex_credentials, jellyfin_credentials, legacy_config, media_root)

        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        _cleanup_outputs(canonical, legacy_config.plex_config_folder)

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=legacy_config,
                # No server_id_filter — universal /api/webhooks/incoming behaviour.
            )

            assert result.status is MultiServerStatus.PUBLISHED, result.message
            adapters = {p.adapter_name for p in result.publishers}
            assert adapters == {"plex_bundle", "jellyfin_trickplay"}, (
                f"Universal URL should publish to BOTH owners; got {adapters}"
            )

            # Both outputs landed.
            trickplay_dir = Path(canonical).parent / "trickplay"
            manifest = trickplay_dir / "Test Movie H264 (2024)-320.json"
            assert manifest.exists(), "Jellyfin trickplay manifest should exist when no filter is set"
            data = json.loads(manifest.read_text())
            assert "Trickplay" in data
        finally:
            _cleanup_outputs(canonical, legacy_config.plex_config_folder)

    def test_per_server_pin_to_unowned_server_returns_no_owners(
        self,
        plex_credentials,
        jellyfin_credentials,
        media_root,
        tmp_path,
    ):
        """Pinning to a server that doesn't own the path → NO_OWNERS, not a publish.

        Edge case: the user POSTs a webhook to `/api/webhooks/server/<id>`
        for a file that server doesn't own (because its libraries don't
        cover the path). Expected: clean skip, not a misrouted publish.
        """
        legacy_config = _legacy_config(plex_credentials, tmp_path)
        # Configure Plex with Movies but Jellyfin with TV Shows only.
        # The Plex webhook below targets a Movies file.
        raw_servers = [
            {
                "id": "plex-int-1",
                "type": "plex",
                "name": "Test Plex",
                "enabled": True,
                "url": plex_credentials["PLEX_URL"],
                "auth": {"method": "token", "token": plex_credentials["PLEX_ACCESS_TOKEN"]},
                "server_identity": plex_credentials["PLEX_SERVER_ID"],
                "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/media/Movies"], "enabled": True}],
                "path_mappings": [{"remote_prefix": "/media", "local_prefix": str(media_root)}],
                "output": {
                    "adapter": "plex_bundle",
                    "plex_config_folder": legacy_config.plex_config_folder,
                    "frame_interval": 5,
                },
            },
            {
                "id": "jf-int-1",
                "type": "jellyfin",
                "name": "Test Jellyfin (TV only)",
                "enabled": True,
                "url": jellyfin_credentials["JELLYFIN_URL"],
                "auth": {"method": "api_key", "api_key": jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"]},
                "server_identity": jellyfin_credentials["JELLYFIN_SERVER_ID"],
                # Only TV Shows library — does NOT cover Movies.
                "libraries": [
                    {"id": "tv", "name": "TV Shows", "remote_paths": ["/jf-media/TV Shows"], "enabled": True}
                ],
                "path_mappings": [{"remote_prefix": "/jf-media", "local_prefix": str(media_root)}],
                "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 5},
            },
        ]
        registry = ServerRegistry.from_settings(raw_servers, legacy_config=legacy_config)

        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        _cleanup_outputs(canonical, legacy_config.plex_config_folder)

        try:
            ffmpeg_calls: list[str] = []
            original_generate = ms_module.generate_images

            def _spy(*args, **kwargs):
                ffmpeg_calls.append(args[0])
                return original_generate(*args, **kwargs)

            ms_module.generate_images = _spy
            try:
                result = process_canonical_path(
                    canonical_path=canonical,
                    registry=registry,
                    config=legacy_config,
                    server_id_filter="jf-int-1",  # Jellyfin doesn't own Movies
                )
            finally:
                ms_module.generate_images = original_generate

            assert result.status is MultiServerStatus.NO_OWNERS, (
                f"Pinning to a non-owning server should yield NO_OWNERS; got {result.status}"
            )
            assert len(ffmpeg_calls) == 0, "Should not invoke FFmpeg when no publisher owns the path"
        finally:
            _cleanup_outputs(canonical, legacy_config.plex_config_folder)
