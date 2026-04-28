"""Webhook flood test: 50 distinct files arriving in parallel.

Different from ``test_five_concurrent_webhooks_run_ffmpeg_once`` —
that test fires 5 webhooks for the *same* file and asserts they
coalesce into one FFmpeg pass. This test fires many webhooks for
*different* files and asserts they each ran their own FFmpeg pass
without serializing on a global lock.

The key invariant: ``FrameCache.generation_lock`` is per-canonical-path,
so two different files should produce two FFmpeg invocations executing
in parallel (limited only by the worker pool's CPU thread count).
A regression that introduced a global lock — say, a misplaced module-
level threading.Lock — would coalesce all 50 to 1 FFmpeg call, which
this test catches.

Files are tiny synthetic clips so 50 of them generate fast even on a
slow runner.
"""

from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BIF_MAGIC = bytes([0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A])

_FLOOD_COUNT = 50


@pytest.fixture
def flood_media_dir(media_root, tmp_path):
    """Generate ``_FLOOD_COUNT`` distinct test clips under tmp_path/Movies."""
    src = media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv"
    movies = tmp_path / "Movies"
    movies.mkdir(parents=True)
    paths = []
    for i in range(_FLOOD_COUNT):
        # Cheap "different file" — copy + truncate-then-rewrite to give
        # each a slightly different size so journals can't accidentally
        # short-circuit. Faster than running FFmpeg 50 times.
        sub = movies / f"Flood Test {i:02d} (2024)"
        sub.mkdir()
        dst = sub / f"Flood Test {i:02d} (2024).mkv"
        shutil.copyfile(src, dst)
        # Append a unique tag so size + content differ per file.
        with dst.open("ab") as f:
            f.write(b"FLOOD" + str(i).zfill(4).encode())
        paths.append(dst)
    yield movies, paths
    if movies.exists():
        shutil.rmtree(movies, ignore_errors=True)


@pytest.fixture
def flood_config(tmp_path):
    config = MagicMock()
    config.plex_url = ""
    config.plex_token = ""
    config.plex_timeout = 60
    config.plex_libraries = []
    config.plex_config_folder = ""
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


@pytest.fixture
def flood_app(emby_credentials, flood_media_dir, tmp_path, monkeypatch, flood_config):
    """Live Flask app pointed at the per-test tmp media dir."""
    from plex_generate_previews.web.app import create_app
    from plex_generate_previews.web.settings_manager import (
        get_settings_manager,
        reset_settings_manager,
    )

    movies_dir, _ = flood_media_dir

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    reset_settings_manager()
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("WEB_AUTH_TOKEN", "integration-test-token")

    app = create_app(config_dir=str(config_dir))
    app.config["TESTING"] = True

    settings = get_settings_manager()
    settings.set("webhook_secret", "integration-secret")
    settings.set(
        "media_servers",
        [
            {
                "id": "emby-flood",
                "type": "emby",
                "name": "Test Emby (flood)",
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
                "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": str(tmp_path)}],
                "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
            }
        ],
    )
    settings.complete_setup()

    monkeypatch.setattr(
        "plex_generate_previews.web.webhook_router._load_config_or_minimal",
        lambda: flood_config,
    )
    return app


@pytest.mark.integration
@pytest.mark.slow
class TestWebhookFloodAcrossDistinctFiles:
    """50 different files arriving simultaneously — verify per-path lock isn't global."""

    def test_fifty_parallel_webhooks_each_run_ffmpeg(self, flood_app, flood_media_dir):
        movies_dir, paths = flood_media_dir

        # Reset frame cache so previous tests can't shortcut us.
        from plex_generate_previews.processing import frame_cache as fc_module

        fc_module._singleton = None  # noqa: SLF001 — test fixture reset

        from plex_generate_previews.processing import multi_server as ms_module

        original_generate = ms_module.generate_images
        ffmpeg_calls: list[str] = []

        def _spy(*args, **kwargs):
            ffmpeg_calls.append(args[0])
            return original_generate(*args, **kwargs)

        ms_module.generate_images = _spy

        try:
            client = flood_app.test_client()

            def _fire(canonical_path: Path):
                return client.post(
                    "/api/webhooks/incoming",
                    headers={"X-Auth-Token": "integration-secret", "Content-Type": "application/json"},
                    data=json.dumps({"path": str(canonical_path), "trigger": "flood"}),
                )

            with ThreadPoolExecutor(max_workers=16) as pool:
                responses = list(pool.map(_fire, paths))

            statuses = [r.status_code for r in responses]
            assert all(s == 200 for s in statuses), f"some webhooks returned non-200: {statuses}"

            # 50 different files → 50 distinct FFmpeg invocations. If a
            # global lock crept in, this would be 1.
            unique_ffmpeg_inputs = set(ffmpeg_calls)
            assert len(unique_ffmpeg_inputs) == _FLOOD_COUNT, (
                f"expected {_FLOOD_COUNT} distinct FFmpeg invocations, got {len(unique_ffmpeg_inputs)}; "
                f"this means a global lock serialized unrelated work"
            )

            # All 50 sidecars should land on disk.
            sidecar_count = 0
            for p in paths:
                stem = p.stem
                sidecar = p.parent / f"{stem}-320-5.bif"
                if sidecar.exists():
                    head = sidecar.read_bytes()[:8]
                    assert head == _BIF_MAGIC, f"{sidecar} has bad BIF magic"
                    sidecar_count += 1
            assert sidecar_count == _FLOOD_COUNT, f"only {sidecar_count}/{_FLOOD_COUNT} sidecars landed"
        finally:
            ms_module.generate_images = original_generate
            # tmp_path teardown removes everything, but be tidy.
