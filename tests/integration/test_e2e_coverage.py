"""Coverage scenarios that go beyond the headline three-server fan-out.

Each test here corresponds to one of the gaps identified in the
"other setups that need testing" review:

* TV show paths (``Show/Season X/SxxExx.mkv``) — most users run this
  layout, our existing tests only covered Movies.
* Source file deleted between webhook and FFmpeg.
* Path-mapping edge cases (trailing slash, prefix collision).
* HEVC source codec.
* Force-regenerate flag actually re-runs FFmpeg.
* Two Plex servers configured simultaneously (multi-instance support
  from issue #215 — schema supports it; this test proves it works).
* Settings migration: legacy single-Plex ``settings.json`` →
  ``media_servers`` array.
"""

from __future__ import annotations

import struct
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plex_generate_previews.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from plex_generate_previews.servers import ServerRegistry

_BIF_MAGIC = bytes([0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A])


def _decode_bif_count(path: Path) -> int:
    raw = path.read_bytes()
    assert raw[:8] == _BIF_MAGIC
    return struct.unpack("<I", raw[12:16])[0]


@pytest.fixture
def coverage_config(tmp_path):
    config = MagicMock()
    config.plex_url = ""
    config.plex_token = ""
    config.plex_timeout = 60
    config.plex_libraries = []
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


def _emby_only_registry(emby_credentials, media_root, library_remote_path: str = "/em-media"):
    raw_servers = [
        {
            "id": "emby-cov",
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
                    "remote_paths": [f"{library_remote_path}/Movies", f"{library_remote_path}/TV Shows"],
                    "enabled": True,
                }
            ],
            "path_mappings": [{"remote_prefix": library_remote_path, "local_prefix": str(media_root)}],
            "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
        }
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=None)


@pytest.mark.integration
class TestTVShowPaths:
    """``Show/Season X/SxxExx.mkv`` is the canonical Sonarr layout."""

    def test_tv_episode_publishes_to_owning_server(self, emby_credentials, media_root, coverage_config):
        canonical = str(media_root / "TV Shows" / "Test Show" / "Season 01" / "Test Show - S01E01 - Pilot.mkv")
        sidecar = Path(canonical).parent / "Test Show - S01E01 - Pilot-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        registry = _emby_only_registry(emby_credentials, media_root)

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=coverage_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            assert sidecar.exists()
            assert _decode_bif_count(sidecar) >= 4
        finally:
            if sidecar.exists():
                sidecar.unlink()
            for f in sidecar.parent.glob("*.bif.meta"):
                f.unlink()


@pytest.mark.integration
class TestForceRegenerate:
    """``regenerate=True`` ignores the journal and re-runs FFmpeg."""

    def test_regenerate_runs_ffmpeg_even_with_fresh_outputs(self, emby_credentials, media_root, coverage_config):
        from plex_generate_previews.processing import frame_cache as fc_module
        from plex_generate_previews.processing import multi_server as ms_module

        fc_module._singleton = None  # noqa: SLF001 — test override

        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        registry = _emby_only_registry(emby_credentials, media_root)

        original_generate = ms_module.generate_images
        ffmpeg_calls = []

        def _spy(*args, **kwargs):
            ffmpeg_calls.append(args[0])
            return original_generate(*args, **kwargs)

        ms_module.generate_images = _spy
        try:
            # Initial publish stamps the journal.
            result = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=coverage_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert result.status is MultiServerStatus.PUBLISHED
            assert len(ffmpeg_calls) == 1

            # Drop the singleton so the cache is "expired" — without
            # regenerate=True, the journal short-circuit would now skip
            # FFmpeg.
            fc_module._singleton = None  # noqa: SLF001

            # With regenerate=True, FFmpeg MUST run again.
            forced = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=coverage_config,
                gpu=None,
                gpu_device_path=None,
                regenerate=True,
            )
            assert forced.status is MultiServerStatus.PUBLISHED
            assert len(ffmpeg_calls) == 2, "regenerate=True must bypass the journal short-circuit"
        finally:
            ms_module.generate_images = original_generate
            if sidecar.exists():
                sidecar.unlink()
            for f in sidecar.parent.glob("*.bif.meta"):
                f.unlink()


@pytest.mark.integration
class TestSourceFileDeletedMidFlight:
    """File vanishes between webhook arrival and FFmpeg call."""

    def test_returns_failed_when_source_disappears(self, emby_credentials, media_root, coverage_config, tmp_path):
        # Use a copy in tmp_path so we can delete it without touching the
        # canonical fixture.
        original = media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv"
        movies_dir = tmp_path / "media" / "Movies" / "Ephemeral (2024)"
        movies_dir.mkdir(parents=True)
        ephemeral = movies_dir / "Ephemeral (2024).mkv"
        ephemeral.write_bytes(original.read_bytes())

        # Build a registry whose path mapping points at this temp media root
        registry = _emby_only_registry(emby_credentials, tmp_path / "media", library_remote_path="/em-media")

        # Delete the source BEFORE we dispatch.
        ephemeral.unlink()

        result = process_canonical_path(
            canonical_path=str(ephemeral),
            registry=registry,
            config=coverage_config,
            gpu=None,
            gpu_device_path=None,
        )
        assert result.status is MultiServerStatus.FAILED
        assert "Source file not found" in result.message


@pytest.mark.integration
class TestPathMappingTrailingSlash:
    """Per-server path mapping handles trailing-slash mismatch."""

    def test_trailing_slash_mismatch_still_resolves(self, emby_credentials, media_root, coverage_config):
        # Library remote_paths configured with trailing slash; canonical
        # path has none. Should still match.
        raw_servers = [
            {
                "id": "emby-slash",
                "type": "emby",
                "name": "Test Emby (slash)",
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
                        "remote_paths": ["/em-media/Movies/"],  # trailing slash
                        "enabled": True,
                    }
                ],
                "path_mappings": [
                    {"remote_prefix": "/em-media/", "local_prefix": str(media_root) + "/"}  # both with slash
                ],
                "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
            }
        ]
        registry = ServerRegistry.from_settings(raw_servers, legacy_config=None)

        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=coverage_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            assert sidecar.exists()
        finally:
            if sidecar.exists():
                sidecar.unlink()
            for f in sidecar.parent.glob("*.bif.meta"):
                f.unlink()


@pytest.mark.integration
class TestHEVCSourceCodec:
    """HEVC fixture exercises a codec branch beyond H.264."""

    def test_hevc_source_publishes_normally(self, emby_credentials, media_root, coverage_config):
        canonical = str(media_root / "Movies" / "Test Movie HEVC (2024)" / "Test Movie HEVC (2024).mkv")
        sidecar = Path(canonical).parent / "Test Movie HEVC (2024)-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        registry = _emby_only_registry(emby_credentials, media_root)

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=coverage_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            assert sidecar.exists()
            assert _decode_bif_count(sidecar) >= 4
        finally:
            if sidecar.exists():
                sidecar.unlink()
            for f in sidecar.parent.glob("*.bif.meta"):
                f.unlink()


@pytest.mark.integration
class TestVP9SourceCodec:
    """VP9 source goes through the same generic CPU FFmpeg path; smoke-tests it works."""

    def test_vp9_source_publishes_normally(self, emby_credentials, media_root, coverage_config):
        canonical = str(media_root / "Movies" / "Test Movie VP9 (2024)" / "Test Movie VP9 (2024).mkv")
        sidecar = Path(canonical).parent / "Test Movie VP9 (2024)-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        registry = _emby_only_registry(emby_credentials, media_root)

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=coverage_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            assert sidecar.exists()
            assert _decode_bif_count(sidecar) >= 4
        finally:
            if sidecar.exists():
                sidecar.unlink()
            for f in sidecar.parent.glob("*.bif.meta"):
                f.unlink()


@pytest.mark.integration
class TestAV1SourceCodec:
    """AV1 source goes through the same generic CPU FFmpeg path; smoke-tests it works."""

    def test_av1_source_publishes_normally(self, emby_credentials, media_root, coverage_config):
        canonical = str(media_root / "Movies" / "Test Movie AV1 (2024)" / "Test Movie AV1 (2024).mkv")
        sidecar = Path(canonical).parent / "Test Movie AV1 (2024)-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        registry = _emby_only_registry(emby_credentials, media_root)

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=coverage_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            assert sidecar.exists()
            assert _decode_bif_count(sidecar) >= 4
        finally:
            if sidecar.exists():
                sidecar.unlink()
            for f in sidecar.parent.glob("*.bif.meta"):
                f.unlink()


@pytest.mark.integration
class TestPathMappingPrefixCollision:
    """Two libraries with overlapping remote prefixes — longer prefix wins."""

    def test_more_specific_library_match_wins(self, emby_credentials, media_root, coverage_config):
        """Two libraries: ``/em-media`` (broad) + ``/em-media/Movies`` (specific).

        A canonical path under ``/em-media/Movies`` should match against
        the specific library, not silently fall through to the broad
        one. We verify by configuring two libraries with different
        ``id``s and asserting the publish lands once (not twice).
        """
        raw_servers = [
            {
                "id": "emby-collide",
                "type": "emby",
                "name": "Test Emby (collide)",
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
                        "id": "broad",
                        "name": "Broad Library",
                        "remote_paths": ["/em-media"],
                        "enabled": True,
                    },
                    {
                        "id": "specific",
                        "name": "Movies",
                        "remote_paths": ["/em-media/Movies"],
                        "enabled": True,
                    },
                ],
                "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": str(media_root)}],
                "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
            }
        ]
        registry = ServerRegistry.from_settings(raw_servers, legacy_config=None)

        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=coverage_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            # Same server appears once even though two libraries cover it.
            assert len(result.publishers) == 1, [(p.server_id, p.adapter_name) for p in result.publishers]
            assert sidecar.exists()
        finally:
            if sidecar.exists():
                sidecar.unlink()
            for f in sidecar.parent.glob("*.bif.meta"):
                f.unlink()


@pytest.mark.integration
@pytest.mark.real_plex_server
class TestTwoPlexServersSameMedia:
    """Two Plex servers in the registry, both configured for the same media.

    The schema supports ``len(media_servers) > 1`` and the path-centric
    pipeline naturally fans out. This test exists to lock in the
    multi-Plex case requested in issue #215.
    """

    def test_two_plex_both_publish(self, plex_credentials, media_root, coverage_config, tmp_path):
        # Two Plex configs pointing at the same backing server (CI has
        # one Plex container) but with distinct ids and distinct
        # plex_config_folder mounts. The publisher fans out to both.
        plex_a_cfg = tmp_path / "plex_a"
        plex_b_cfg = tmp_path / "plex_b"
        plex_a_cfg.mkdir()
        plex_b_cfg.mkdir()

        raw_servers = [
            {
                "id": "plex-a",
                "type": "plex",
                "name": "Plex Alpha",
                "enabled": True,
                "url": plex_credentials["PLEX_URL"],
                "auth": {"method": "token", "token": plex_credentials["PLEX_ACCESS_TOKEN"]},
                "server_identity": plex_credentials["PLEX_SERVER_ID"] + "-a",
                "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/media/Movies"], "enabled": True}],
                "path_mappings": [{"remote_prefix": "/media", "local_prefix": str(media_root)}],
                "output": {
                    "adapter": "plex_bundle",
                    "plex_config_folder": str(plex_a_cfg),
                    "frame_interval": 5,
                },
            },
            {
                "id": "plex-b",
                "type": "plex",
                "name": "Plex Bravo",
                "enabled": True,
                "url": plex_credentials["PLEX_URL"],
                "auth": {"method": "token", "token": plex_credentials["PLEX_ACCESS_TOKEN"]},
                "server_identity": plex_credentials["PLEX_SERVER_ID"] + "-b",
                "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/media/Movies"], "enabled": True}],
                "path_mappings": [{"remote_prefix": "/media", "local_prefix": str(media_root)}],
                "output": {
                    "adapter": "plex_bundle",
                    "plex_config_folder": str(plex_b_cfg),
                    "frame_interval": 5,
                },
            },
        ]
        # Need a legacy config for the Plex client - reuse coverage_config but
        # populate the Plex fields.
        coverage_config.plex_url = plex_credentials["PLEX_URL"]
        coverage_config.plex_token = plex_credentials["PLEX_ACCESS_TOKEN"]
        registry = ServerRegistry.from_settings(raw_servers, legacy_config=coverage_config)

        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        result = process_canonical_path(
            canonical_path=canonical,
            registry=registry,
            config=coverage_config,
            gpu=None,
            gpu_device_path=None,
        )

        try:
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            # Both Plex publishers should fire.
            plex_publishers = [p for p in result.publishers if p.adapter_name == "plex_bundle"]
            assert len(plex_publishers) == 2, [p.server_id for p in plex_publishers]
            assert {p.status for p in plex_publishers} == {PublisherStatus.PUBLISHED}
            # And they wrote into different config folders.
            paths = [p.output_paths[0] for p in plex_publishers]
            assert str(plex_a_cfg) in str(paths[0]) or str(plex_a_cfg) in str(paths[1])
            assert str(plex_b_cfg) in str(paths[0]) or str(plex_b_cfg) in str(paths[1])
        finally:
            for p in (plex_a_cfg, plex_b_cfg):
                for bif in p.rglob("index-sd.bif"):
                    bif.unlink()


@pytest.mark.integration
class TestSettingsMigrationLegacyPlex:
    """Legacy single-Plex settings.json migrates to media_servers[0]."""

    def test_legacy_plex_fields_become_first_media_server(self, tmp_path):
        """Legacy single-Plex settings.json migrates to media_servers[0].

        Exercises the v7 schema migration in :mod:`upgrade.py` end-to-end.
        Pre-existing single-Plex users should land on a populated
        ``media_servers`` list with no manual edits.
        """
        from plex_generate_previews.upgrade import run_migrations
        from plex_generate_previews.web.settings_manager import SettingsManager

        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        legacy = (
            '{"plex_url": "http://legacy:32400",'
            ' "plex_token": "legacy-tok",'
            ' "plex_libraries": ["Movies"],'
            ' "plex_config_folder": "/legacy/config"}'
        )
        (config_dir / "settings.json").write_text(legacy)

        manager = SettingsManager(str(config_dir))
        # Migration runs on run_migrations (also wired into app startup).
        run_migrations(manager)

        servers = manager.get("media_servers")
        assert isinstance(servers, list), f"media_servers should be a list, got {type(servers).__name__}"
        assert len(servers) == 1, f"expected exactly one migrated server, got {len(servers)}"
        plex = servers[0]
        assert plex["type"] == "plex"
        assert plex["url"] == "http://legacy:32400"
        token = plex.get("auth", {}).get("token") or plex.get("auth", {}).get("access_token") or ""
        assert token == "legacy-tok", plex.get("auth")
