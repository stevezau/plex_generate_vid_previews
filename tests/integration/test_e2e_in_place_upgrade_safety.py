"""End-to-end test: in-place upgrade does not delete live previews.

Bug shape this test was written to catch (live regression 2026-05-09
against EmbyTest with Gary (2026)):

  * Radarr's ``Download`` webhook for an in-place upgrade (same
    filename overwritten with new content) lists the path of the
    OVERWRITTEN file in ``deletedFiles[]``. From Radarr's POV, the
    OLD file at that path is "deleted" — but the path now hosts the
    NEW content.
  * Pre-fix our cleanup happily deleted the new file's sidecars
    because the basename matched the entry in ``deletedFiles[]``.
    The atomic-swap publisher had just written ``Movie.trickplay/``
    and ``Movie-320-10.bif`` next to ``Movie.mkv``; cleanup wiped
    them milliseconds later.
  * The retry queue then re-extracted + re-published, masking the
    data loss in the slow path. Users only noticed when retries
    weren't running (e.g. if BACKOFF exhausted).

Mitigation: the cleanup pass and the deleted-path nudge BOTH skip a
``deleted_path`` if either:
  (a) The file still exists on disk (``os.path.exists`` returns True), OR
  (b) Its basename equals the canonical_path's basename (covers
      path-mapping edge cases where the foreign mount might not be
      visible from this process but still resolves to the live file).

This test exercises both safety guards end-to-end against the test
stack: publish, then dispatch a synthetic in-place-upgrade webhook,
then assert the live artifacts are STILL on disk.

Without the fix, this test would pass-then-fail: artifacts present
after publish, then deleted by the upgrade dispatch's cleanup pass.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    process_canonical_path,
)
from media_preview_generator.servers import ServerRegistry


@pytest.fixture
def upgrade_safety_config(tmp_path):
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
def upgrade_safety_registry(emby_credentials, jellyfin_credentials, media_root):
    """Both Emby + Jellyfin so we exercise both adapter cleanup paths."""
    raw_servers = [
        {
            "id": "emby-upgrade",
            "type": "emby",
            "name": "Test Emby (upgrade safety)",
            "enabled": True,
            "url": emby_credentials["EMBY_URL"],
            "auth": {
                "method": "password",
                "access_token": emby_credentials["EMBY_ACCESS_TOKEN"],
                "user_id": emby_credentials["EMBY_USER_ID"],
            },
            "server_identity": emby_credentials["EMBY_SERVER_ID"],
            "libraries": [{"id": "movies", "name": "Movies", "remote_paths": ["/em-media/Movies"], "enabled": True}],
            "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": str(media_root)}],
            "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
        },
        {
            "id": "jelly-upgrade",
            "type": "jellyfin",
            "name": "Test Jellyfin (upgrade safety)",
            "enabled": True,
            "url": jellyfin_credentials["JELLYFIN_URL"],
            "auth": {
                "method": "api_key",
                "api_key": jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"],
            },
            "server_identity": jellyfin_credentials["JELLYFIN_SERVER_ID"],
            "libraries": [{"id": "movies", "name": "Movies", "remote_paths": ["/jf-media/Movies"], "enabled": True}],
            "path_mappings": [{"remote_prefix": "/jf-media", "local_prefix": str(media_root)}],
            "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 5},
        },
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=None)


@pytest.mark.integration
class TestInPlaceUpgradeDoesNotDeleteLivePreviews:
    """Regression: simulate Radarr's deletedFiles=[same path as new file]
    and assert the live artifacts SURVIVE.

    Pre-fix this test would fail: dispatch publishes BIF + trickplay,
    then cleanup deletes them because the basename matches the
    "deleted" path.
    """

    def test_dispatch_with_deletedfiles_echoing_new_path_preserves_artifacts(
        self,
        upgrade_safety_registry,
        upgrade_safety_config,
        media_root,
    ):
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        sidecar_meta = sidecar.with_suffix(sidecar.suffix + ".meta")
        trickplay = Path(canonical).parent / "Test Movie H264 (2024).trickplay"
        # Clean any leftover artifacts from previous runs.
        if sidecar.exists():
            sidecar.unlink()
        if sidecar_meta.exists():
            sidecar_meta.unlink()
        if trickplay.exists():
            import shutil

            shutil.rmtree(trickplay)

        try:
            # ----- Step 1: initial publish writes the artifacts -----
            initial = process_canonical_path(
                canonical_path=canonical,
                registry=upgrade_safety_registry,
                config=upgrade_safety_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert initial.status is MultiServerStatus.PUBLISHED, initial.message
            assert sidecar.exists(), "Initial publish failed to write Emby BIF sidecar"
            assert trickplay.exists(), "Initial publish failed to write Jellyfin trickplay dir"

            # Snapshot the artifact mtimes so we can assert they
            # weren't recreated (which would mask the bug — see the
            # docstring's note about retry-queue masking).
            sidecar_inode_pre = sidecar.stat().st_ino
            trickplay_inode_pre = trickplay.stat().st_ino

            # ----- Step 2: dispatch the synthetic in-place upgrade -----
            # ``deletedFiles=[canonical]`` simulates Radarr's payload
            # for an in-place upgrade. Pre-fix this would trigger the
            # cleanup pass to remove the live artifacts because their
            # basenames match the "deleted" path's basename.
            upgrade = process_canonical_path(
                canonical_path=canonical,
                registry=upgrade_safety_registry,
                config=upgrade_safety_config,
                gpu=None,
                gpu_device_path=None,
                deleted_paths=[canonical],
                # Don't schedule retries — we're testing the cleanup
                # pass, not the retry chain.
                schedule_retry_on_not_indexed=False,
            )
            assert upgrade.status is not MultiServerStatus.FAILED, upgrade.message

            # ----- Step 3: live artifacts MUST survive -----
            assert sidecar.exists(), (
                "Emby BIF sidecar was deleted by the in-place upgrade dispatch. "
                "The cleanup safety guard (os.path.exists check on deleted_paths) failed — "
                "Radarr's deletedFiles[] echo of the new path tricked us into wiping "
                "the live BIF. This is the Gary (2026) catastrophe regression."
            )
            assert trickplay.exists(), (
                "Jellyfin trickplay directory was deleted by the in-place upgrade dispatch. "
                "Same root cause as the BIF case — the deleted-path safety guard didn't fire."
            )
            # Inode unchanged → file wasn't deleted-and-recreated by the
            # retry's regenerate. Catches the silent-mask scenario where
            # cleanup deletes, then a publish/retry recreates with the
            # same basename — pre-fix this hid the data-loss for hours.
            assert sidecar.stat().st_ino == sidecar_inode_pre, (
                f"BIF sidecar inode changed ({sidecar_inode_pre} → {sidecar.stat().st_ino}) — "
                f"the file was deleted-and-recreated, masking a real cleanup-deletion bug. "
                f"The in-place upgrade safety guard MUST prevent the deletion entirely; "
                f"recreation by retry is masking, not fixing."
            )
            assert trickplay.stat().st_ino == trickplay_inode_pre, (
                f"Trickplay dir inode changed ({trickplay_inode_pre} → {trickplay.stat().st_ino}) "
                f"— same masking pattern as the BIF case above."
            )
        finally:
            if sidecar.exists():
                sidecar.unlink()
            if sidecar_meta.exists():
                sidecar_meta.unlink()
            if trickplay.exists():
                import shutil

                shutil.rmtree(trickplay)
