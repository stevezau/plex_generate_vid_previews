"""Live e2e test for the Jellyfin trickplay-flag misconfig + auto-fix.

The "preview file is on disk" guarantee isn't enough — Jellyfin
defaults ``EnableTrickplayImageExtraction`` to false, and with that
flag off it ignores our sidecar trickplay even when everything else
is correct. The user sees no scrubbing thumbnails and reports the
tool as broken.

This test verifies:

1. The unified :meth:`MediaServer.check_settings_health` audit detects
   the ``EnableTrickplayImageExtraction`` misconfiguration on the
   live Jellyfin (one of four flags it surfaces — see
   :data:`JellyfinServer._RECOMMENDED_SETTINGS`).
2. :meth:`MediaServer.apply_recommended_settings` flips the flag.
3. After the flip + a library refresh, Jellyfin populates the
   ``Trickplay`` field on the item AND serves the tile sheet via its
   HTTP API (the byte-stream the web UI fetches when scrubbing).

We restore the original library settings at the end so other tests
aren't affected.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from media_preview_generator.processing.multi_server import process_canonical_path
from media_preview_generator.servers import ServerRegistry


@pytest.fixture
def jf_dedup_config(tmp_path):
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
def jf_registry(jellyfin_credentials, media_root):
    raw_servers = [
        {
            "id": "jf-trickfix",
            "type": "jellyfin",
            "name": "Test Jellyfin",
            "enabled": True,
            "url": jellyfin_credentials["JELLYFIN_URL"],
            "auth": {"method": "api_key", "api_key": jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"]},
            "server_identity": jellyfin_credentials["JELLYFIN_SERVER_ID"],
            "libraries": [{"id": "movies", "name": "Movies", "remote_paths": ["/jf-media/Movies"], "enabled": True}],
            "path_mappings": [{"remote_prefix": "/jf-media", "local_prefix": str(media_root)}],
            "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 5},
        }
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=None)


@pytest.fixture
def restore_jellyfin_library_options(jellyfin_credentials):
    """Snapshot the JF library options so we can restore them after the test."""
    jf_url = jellyfin_credentials["JELLYFIN_URL"]
    jf_token = jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"]

    snapshot = requests.get(
        f"{jf_url}/Library/VirtualFolders",
        headers={"X-Emby-Token": jf_token},
        timeout=10,
    ).json()
    yield
    for folder in snapshot:
        if not isinstance(folder, dict):
            continue
        lib_id = folder.get("ItemId") or folder.get("Id")
        if not lib_id:
            continue
        requests.post(
            f"{jf_url}/Library/VirtualFolders/LibraryOptions",
            headers={"X-Emby-Token": jf_token, "Content-Type": "application/json"},
            json={"Id": lib_id, "LibraryOptions": folder.get("LibraryOptions") or {}},
            timeout=10,
        )


@pytest.mark.integration
class TestJellyfinTrickplayMisconfigDetection:
    def test_detects_disabled_trickplay_extraction(self, jf_registry):
        """check_settings_health surfaces the EnableTrickplayImageExtraction issue.

        Default Jellyfin libraries ship with ``EnableTrickplayImageExtraction=False``
        — the unified health-check must flag it as a *critical* issue (the
        flag is destructive when off; Jellyfin DELETES our published
        trickplay on the next refresh).
        """
        server = jf_registry.get("jf-trickfix")
        issues = server.check_settings_health()
        assert issues, (
            "Jellyfin returned no health-check issues — expected at least one for the default-off trickplay flag"
        )
        # Critical issue surfaced for the destructive flag.
        critical = [i for i in issues if i.flag == "EnableTrickplayImageExtraction"]
        assert critical, f"expected EnableTrickplayImageExtraction in issues, got {[i.flag for i in issues]}"
        assert critical[0].severity == "critical"
        assert critical[0].fixable is True


@pytest.mark.integration
@pytest.mark.slow
class TestJellyfinTrickplayAutoFixEndToEnd:
    """The full user-visible loop: publish, fix, verify Jellyfin actually serves it."""

    def test_after_fix_jellyfin_serves_trickplay_through_api(
        self,
        jf_registry,
        jf_dedup_config,
        media_root,
        jellyfin_credentials,
        restore_jellyfin_library_options,
    ):
        """E2E: misconfig → publish (no thumbs) → enable → JF serves tile sheet."""
        jf_url = jellyfin_credentials["JELLYFIN_URL"]
        jf_token = jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"]
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        trickplay_dir = Path(canonical).parent / "trickplay"
        if trickplay_dir.exists():
            import shutil

            shutil.rmtree(trickplay_dir)

        server = jf_registry.get("jf-trickfix")

        # ----- 1. Publish — produces files on disk -----
        result = process_canonical_path(
            canonical_path=canonical,
            registry=jf_registry,
            config=jf_dedup_config,
            gpu=None,
            gpu_device_path=None,
        )
        assert result.publishers, "no publishers ran"
        assert (trickplay_dir / "Test Movie H264 (2024)-320.json").exists()

        try:
            # ----- 2. Apply the fix via the unified health-check -----
            issues_before = server.check_settings_health()
            critical_before = [i for i in issues_before if i.flag == "EnableTrickplayImageExtraction"]
            assert critical_before, (
                f"expected EnableTrickplayImageExtraction issue, got {[i.flag for i in issues_before]}"
            )

            fix_results = server.apply_recommended_settings(flags=["EnableTrickplayImageExtraction"])
            assert all(v == "ok" for v in fix_results.values()), fix_results

            issues_after = server.check_settings_health()
            critical_after = [i for i in issues_after if i.flag == "EnableTrickplayImageExtraction"]
            assert not critical_after, f"flag still mis-set after fix: {critical_after}"

            # ----- 3. Trigger a refresh + poll for JF to pick up the trickplay -----
            requests.post(
                f"{jf_url}/Library/Refresh",
                headers={"X-Emby-Token": jf_token},
                timeout=10,
            )

            # Find the item id (bare /Items/{id} 400s on JF; use wrapped)
            items = requests.get(
                f"{jf_url}/Items",
                headers={"X-Emby-Token": jf_token},
                params={
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie",
                    "Fields": "Path",
                    "Limit": 50,
                },
                timeout=10,
            ).json()["Items"]
            jf_item = next((i for i in items if "Test Movie H264 (2024).mkv" in (i.get("Path") or "")), None)
            assert jf_item, "Jellyfin doesn't see the test movie"
            item_id = jf_item["Id"]

            # Force an item-targeted refresh after enabling.
            requests.post(
                f"{jf_url}/Items/{item_id}/Refresh",
                headers={"X-Emby-Token": jf_token},
                params={
                    "Recursive": "true",
                    "MetadataRefreshMode": "FullRefresh",
                    "ImageRefreshMode": "FullRefresh",
                },
                timeout=10,
            )

            seen_trickplay = False
            for _ in range(30):
                time.sleep(2)
                r = requests.get(
                    f"{jf_url}/Items",
                    headers={"X-Emby-Token": jf_token},
                    params={"Ids": item_id, "Fields": "Trickplay"},
                    timeout=10,
                )
                if r.status_code != 200:
                    continue
                body = r.json()
                if not body.get("Items"):
                    continue
                tp = body["Items"][0].get("Trickplay")
                if tp:
                    # Jellyfin's response Trickplay shape is
                    # ``{<item_id>: {<width>: {...metadata...}}}`` —
                    # mirroring the on-disk manifest. Drill down to the
                    # innermost width-keyed dict.
                    by_width = next(iter(tp.values())) if tp else {}
                    if not isinstance(by_width, dict) or not by_width:
                        continue
                    info = by_width.get("320") or next(iter(by_width.values()))
                    if not isinstance(info, dict) or "TileWidth" not in info:
                        continue
                    seen_trickplay = True
                    assert info["TileWidth"] == 10
                    assert info["TileHeight"] == 10
                    assert info["ThumbnailCount"] > 0
                    break

            assert seen_trickplay, "Jellyfin did not register the Trickplay metadata even after the auto-fix + refresh"

            # ----- 4. The actual UI proof: JF serves the tile sheet over HTTP -----
            # Jellyfin's metadata endpoint registers Trickplay before the
            # internal sheet route is wired up to serve it. Poll briefly.
            sheet = None
            for _ in range(15):
                sheet = requests.get(
                    f"{jf_url}/Videos/{item_id}/Trickplay/320/0.jpg",
                    headers={"X-Emby-Token": jf_token},
                    timeout=10,
                )
                if sheet.status_code == 200:
                    break
                time.sleep(2)
            assert sheet is not None and sheet.status_code == 200, (
                f"Jellyfin still 404s the tile sheet after 30s: {sheet.status_code if sheet else 'no response'}"
            )
            assert sheet.content[:2] == b"\xff\xd8", "served bytes are not a JPEG"
            assert len(sheet.content) > 0
        finally:
            if trickplay_dir.exists():
                import shutil

                shutil.rmtree(trickplay_dir)
