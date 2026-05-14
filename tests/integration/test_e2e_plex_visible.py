"""End-to-end test: Plex actually serves the BIF we publish.

Symmetric to ``test_e2e_jellyfin_trickplay_fix.py`` for Jellyfin —
verifies the live Plex Media Server's web client could fetch and
display preview thumbnails after we publish.

The challenge: the test fixture's ``plex_config_folder`` points at a
per-test tmp dir Plex never sees. So:

1. Run ``process_canonical_path`` → BIF lands at the test tmp dir at
   ``{plex_config}/Media/localhost/<h0>/<h[1:]>.bundle/Contents/Indexes/index-sd.bif``.
2. ``docker cp`` the whole bundle directory into the live Plex
   container's real config volume.
3. Trigger a Plex partial scan so Plex re-reads its bundle directory.
4. Fetch ``/library/parts/{partId}/indexes/sd/{offset}`` from Plex's
   HTTP API — this is the byte-stream Plex serves to the web player
   when scrubbing. Assert it returns a valid JPEG.

Skip if the test Plex container isn't reachable.
"""

from __future__ import annotations

import struct
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    process_canonical_path,
)
from media_preview_generator.servers import ServerRegistry

_BIF_MAGIC = bytes([0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A])
_JPEG_SOI = bytes([0xFF, 0xD8, 0xFF])

# Plex's docker-compose container name + the in-container config dir.
PLEX_CONTAINER = "previews-test-plex"
PLEX_IN_CONTAINER_CONFIG = "/config/Library/Application Support/Plex Media Server"


def _docker_cp(local_path: Path, container_dest: str) -> None:
    """Copy a local file/directory into the live Plex container.

    Mirrors the existing ``docker exec`` shell-out pattern in
    ``setup_servers.py``. Preserves directory structure when ``local_path``
    is a directory.
    """
    subprocess.run(
        ["docker", "cp", str(local_path), f"{PLEX_CONTAINER}:{container_dest}"],
        check=True,
        capture_output=True,
    )


def _docker_exec(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", PLEX_CONTAINER, *args],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def plex_visible_config(plex_credentials, tmp_path):
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


@pytest.fixture
def plex_visible_registry(plex_credentials, plex_visible_config, media_root):
    raw_servers = [
        {
            "id": "plex-visible",
            "type": "plex",
            "name": "Test Plex (visible)",
            "enabled": True,
            "url": plex_credentials["PLEX_URL"],
            "auth": {"method": "token", "token": plex_credentials["PLEX_ACCESS_TOKEN"]},
            "server_identity": plex_credentials["PLEX_SERVER_ID"],
            "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/media/Movies"], "enabled": True}],
            "path_mappings": [{"remote_prefix": "/media", "local_prefix": str(media_root)}],
            "output": {
                "adapter": "plex_bundle",
                "plex_config_folder": str(plex_visible_config.plex_config_folder),
                "frame_interval": 5,
            },
        }
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=plex_visible_config)


def _bundle_subpath_from_test_path(test_bif_path: Path, plex_config_root: str) -> str:
    """Extract ``Media/localhost/<h0>/<...>.bundle`` from the test's BIF path.

    Returns the path relative to plex_config_root so we can mirror it
    inside the container.
    """
    test_str = str(test_bif_path)
    root_str = str(plex_config_root)
    assert test_str.startswith(root_str), f"BIF path {test_str} not under root {root_str}"
    # bundle root is the directory containing "Contents/Indexes/index-sd.bif"
    rel = Path(test_str[len(root_str) :].lstrip("/"))
    # Drop trailing 3 components: index-sd.bif, Indexes, Contents
    return str(rel.parent.parent.parent)  # Media/localhost/<h0>/<h[1:]>.bundle


@pytest.mark.integration
@pytest.mark.real_plex_server
@pytest.mark.slow
class TestPlexServesPublishedBif:
    """Real proof: Plex returns valid JPEG bytes when its API serves our BIF."""

    def test_plex_serves_bif_thumbnail_after_publish_and_scan(
        self,
        plex_visible_registry,
        plex_visible_config,
        plex_credentials,
        media_root,
    ):
        # Skip if docker isn't reachable from this runner.
        try:
            subprocess.run(["docker", "ps", "-q", "-f", f"name={PLEX_CONTAINER}"], check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pytest.skip(f"docker / {PLEX_CONTAINER} not available")

        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")

        # ----- 1. Publish to test plex_config_folder -----
        result = process_canonical_path(
            canonical_path=canonical,
            registry=plex_visible_registry,
            config=plex_visible_config,
            gpu=None,
            gpu_device_path=None,
        )
        assert result.status is MultiServerStatus.PUBLISHED, result.message
        plex_pub = next(p for p in result.publishers if p.adapter_name == "plex_bundle")
        assert plex_pub.status.value == "published", plex_pub.message
        bif_path = plex_pub.output_paths[0]
        assert bif_path.exists()
        # Quick sanity: BIF magic + at least one frame.
        raw = bif_path.read_bytes()
        assert raw[:8] == _BIF_MAGIC
        image_count = struct.unpack("<I", raw[12:16])[0]
        assert image_count > 0

        # ----- 2. docker cp the whole bundle dir into the live Plex container -----
        # Compute Media/localhost/<h0>/<bundle> subpath.
        bundle_subpath = _bundle_subpath_from_test_path(bif_path, plex_visible_config.plex_config_folder)
        # Local source is the .bundle dir (containing Contents/Indexes/index-sd.bif).
        local_bundle = bif_path.parent.parent.parent  # ...bundle/
        # Container destination — parent dir of the .bundle.
        container_parent = f"{PLEX_IN_CONTAINER_CONFIG}/{Path(bundle_subpath).parent}"
        # Ensure the parent exists in the container.
        mkdir_proc = _docker_exec("mkdir", "-p", container_parent)
        assert mkdir_proc.returncode == 0, mkdir_proc.stderr

        _docker_cp(local_bundle, container_parent)

        # Verify the BIF is now inside the container.
        in_container_bif = f"{PLEX_IN_CONTAINER_CONFIG}/{bundle_subpath}/Contents/Indexes/index-sd.bif"
        ls_proc = _docker_exec("ls", "-la", in_container_bif)
        assert ls_proc.returncode == 0, f"BIF not present after docker cp: {ls_proc.stderr}"

        # ----- 3. Find the Plex item id + part id -----
        plex_url = plex_credentials["PLEX_URL"]
        plex_token = plex_credentials["PLEX_ACCESS_TOKEN"]
        sections = requests.get(
            f"{plex_url}/library/sections",
            headers={"X-Plex-Token": plex_token, "Accept": "application/json"},
            timeout=10,
        ).json()
        movies_section = next((s for s in sections["MediaContainer"]["Directory"] if s["title"] == "Movies"), None)
        assert movies_section, "Movies library not found on Plex"

        # Trigger a partial scan so Plex re-reads bundle dirs.
        requests.get(
            f"{plex_url}/library/sections/{movies_section['key']}/refresh",
            headers={"X-Plex-Token": plex_token},
            timeout=10,
        )

        # Wait briefly for Plex to scan, then match by part filename
        # (Plex's metadata agents strip codec suffixes from titles —
        # "Test Movie H264" surfaces as "Test Movie" — so we match on
        # the actual on-disk basename instead).
        rating_key = None
        part_id = None
        target_basename = Path(canonical).name  # "Test Movie H264 (2024).mkv"
        for _ in range(30):
            time.sleep(1)
            items = requests.get(
                f"{plex_url}/library/sections/{movies_section['key']}/all",
                headers={"X-Plex-Token": plex_token, "Accept": "application/json"},
                timeout=10,
            ).json()
            for item in items["MediaContainer"].get("Metadata", []) or []:
                # Need the deep media entry to compare part filenames.
                detail = requests.get(
                    f"{plex_url}/library/metadata/{item['ratingKey']}",
                    headers={"X-Plex-Token": plex_token, "Accept": "application/json"},
                    timeout=10,
                ).json()
                metas = detail["MediaContainer"].get("Metadata", []) or []
                if not metas:
                    continue
                for media in metas[0].get("Media", []) or []:
                    for part in media.get("Part", []) or []:
                        if Path(part.get("file", "")).name == target_basename:
                            rating_key = item["ratingKey"]
                            part_id = part["id"]
                            break
                    if rating_key:
                        break
                if rating_key:
                    break
            if rating_key:
                break

        assert rating_key and part_id, f"Plex didn't surface a movie matching {target_basename!r}"

        # ----- 4. Fetch a thumb byte-stream from Plex's index endpoint -----
        # Plex serves indexed thumbs at /library/parts/{partId}/indexes/sd/{offset}
        # where offset is in seconds. With our 5s frame interval and
        # >0 frames, offset=0 should return a JPEG.
        thumb = requests.get(
            f"{plex_url}/library/parts/{part_id}/indexes/sd/0",
            headers={"X-Plex-Token": plex_token},
            timeout=10,
        )

        assert thumb.status_code == 200, (
            f"Plex didn't serve a thumbnail: HTTP {thumb.status_code}, body={thumb.content[:200]}"
        )
        assert thumb.content[:3] == _JPEG_SOI, f"Plex returned non-JPEG bytes (first 16: {thumb.content[:16].hex()})"
        assert len(thumb.content) > 100, f"Plex returned suspiciously small thumb ({len(thumb.content)} bytes)"
