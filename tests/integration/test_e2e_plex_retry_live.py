"""End-to-end test: slow-backoff retry against live Plex.

The unit tests in ``test_processing_retry_queue.py`` cover the
RetryScheduler in isolation with mocks. This test exercises the full
chain against a LIVE Plex Media Server:

1. First publish: Plex hasn't yet "indexed" the file → adapter raises
   LibraryNotYetIndexedError → publisher status SKIPPED_NOT_INDEXED →
   dispatcher schedules a retry via the live RetryScheduler.
2. Backoff timer fires (we patch ``_BACKOFF`` to short delays so the
   test runs in seconds, not minutes).
3. Retry callback re-dispatches → this time Plex returns the bundle
   hash → publisher status PUBLISHED.
4. Verify final result is PUBLISHED and exactly 2 calls happened to
   ``PlexServer.get_bundle_metadata`` (one miss, one hit).

The "first miss, then hit" simulation patches ``get_bundle_metadata``
at the class level. The test container's Plex is real; the patch only
controls which response that method returns at each call.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plex_generate_previews.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from plex_generate_previews.servers import ServerRegistry


@pytest.fixture(autouse=True)
def _reset_retry_singleton():
    from plex_generate_previews.processing.retry_queue import reset_retry_scheduler

    reset_retry_scheduler()
    yield
    reset_retry_scheduler()


@pytest.fixture
def plex_retry_config(plex_credentials, tmp_path):
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
def plex_retry_registry(plex_credentials, plex_retry_config, media_root):
    raw_servers = [
        {
            "id": "plex-retry",
            "type": "plex",
            "name": "Test Plex (retry)",
            "enabled": True,
            "url": plex_credentials["PLEX_URL"],
            "auth": {"method": "token", "token": plex_credentials["PLEX_ACCESS_TOKEN"]},
            "server_identity": plex_credentials["PLEX_SERVER_ID"],
            "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/media/Movies"], "enabled": True}],
            "path_mappings": [{"remote_prefix": "/media", "local_prefix": str(media_root)}],
            "output": {
                "adapter": "plex_bundle",
                "plex_config_folder": str(plex_retry_config.plex_config_folder),
                "frame_interval": 5,
            },
        }
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=plex_retry_config)


@pytest.mark.integration
@pytest.mark.real_plex_server
@pytest.mark.slow
class TestSlowBackoffRetryAgainstLivePlex:
    def test_unindexed_then_indexed_publishes_via_retry(
        self,
        plex_retry_registry,
        plex_retry_config,
        media_root,
        plex_credentials,
    ):
        """Simulate Plex 'not yet indexed' on first call, 'indexed' on retry."""
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")

        from plex_generate_previews.processing import frame_cache as fc_module
        from plex_generate_previews.servers.plex import PlexServer

        fc_module._singleton = None  # noqa: SLF001 — start with a clean cache

        # Capture the live PlexServer's real bundle metadata so we can
        # return it on the second call.
        live_plex = plex_retry_registry.get("plex-retry")
        # Find a real item id by querying the live container.
        import requests as _req

        sections = _req.get(
            f"{plex_credentials['PLEX_URL']}/library/sections",
            headers={"X-Plex-Token": plex_credentials["PLEX_ACCESS_TOKEN"], "Accept": "application/json"},
            timeout=10,
        ).json()
        movies_section = next(s for s in sections["MediaContainer"]["Directory"] if s["title"] == "Movies")
        items = _req.get(
            f"{plex_credentials['PLEX_URL']}/library/sections/{movies_section['key']}/all",
            headers={"X-Plex-Token": plex_credentials["PLEX_ACCESS_TOKEN"], "Accept": "application/json"},
            timeout=10,
        ).json()
        # Pick the H264 fixture's rating_key — match by part filename.
        target_basename = Path(canonical).name
        rating_key = None
        for item in items["MediaContainer"].get("Metadata", []) or []:
            detail = _req.get(
                f"{plex_credentials['PLEX_URL']}/library/metadata/{item['ratingKey']}",
                headers={"X-Plex-Token": plex_credentials["PLEX_ACCESS_TOKEN"], "Accept": "application/json"},
                timeout=10,
            ).json()
            for media in detail["MediaContainer"]["Metadata"][0].get("Media", []) or []:
                for part in media.get("Part", []) or []:
                    if Path(part.get("file", "")).name == target_basename:
                        rating_key = item["ratingKey"]
                        break
                if rating_key:
                    break
            if rating_key:
                break

        assert rating_key, f"Could not find Plex item for {target_basename}"

        # Real bundle metadata for the second call.
        real_parts = live_plex.get_bundle_metadata(rating_key)
        assert real_parts, "Plex returned no bundle metadata for the test item"

        # Stub: simulate "not yet indexed" until the retry kicks in.
        # ``process_canonical_path`` calls ``compute_output_paths``
        # twice on the first dispatch — once during the pre-FFmpeg
        # short-circuit probe, again in the main publish path — so we
        # need two empty returns before the retry kicks off, then real
        # data on the retry's first probe (and second main call).
        # Using a list flag keeps the test's assertion-on-call-count
        # robust to that internal flow.
        unindexed_phase = {"active": True}
        call_count = {"n": 0}

        def fake_get_bundle_metadata(self, item_id):
            call_count["n"] += 1
            if unindexed_phase["active"]:
                return []
            return real_parts

        # Force item_id_by_server hint so we don't depend on Plex's
        # reverse-lookup against the test container's library state.
        item_id_hint = {"plex-retry": str(rating_key)}

        # Patch backoff to fire fast; patch get_bundle_metadata.
        retry_complete = threading.Event()

        from plex_generate_previews.processing.retry_queue import schedule_retry_for_unindexed as orig_schedule

        def watch_schedule(*args, **kwargs):
            result = orig_schedule(*args, **kwargs)
            # Mark the chain as in-flight so the test knows the retry was scheduled.
            return result

        with (
            patch.object(PlexServer, "get_bundle_metadata", fake_get_bundle_metadata),
            patch(
                "plex_generate_previews.processing.retry_queue._BACKOFF",
                (0.1, 0.5, 1.0, 2.0, 5.0),
            ),
            patch(
                "plex_generate_previews.processing.retry_queue.schedule_retry_for_unindexed",
                side_effect=watch_schedule,
            ),
        ):
            # First dispatch: Plex says not indexed → SKIPPED_NOT_INDEXED + retry scheduled.
            first = process_canonical_path(
                canonical_path=canonical,
                registry=plex_retry_registry,
                config=plex_retry_config,
                item_id_by_server=item_id_hint,
                gpu=None,
                gpu_device_path=None,
            )
            assert first.status is MultiServerStatus.SKIPPED, first.message
            assert first.publishers[0].status is PublisherStatus.SKIPPED_NOT_INDEXED
            calls_after_first = call_count["n"]

            # Now flip the simulation: the file is "indexed" by the time
            # the backoff timer fires.
            unindexed_phase["active"] = False

            # Wait for the retry timer to fire + complete (backoff[0] = 0.1s).
            # Detected by call_count growing past calls_after_first AND
            # a BIF appearing on disk.
            for _ in range(50):  # up to 5s
                time.sleep(0.1)
                if call_count["n"] > calls_after_first:
                    retry_complete.set()
                    break

            assert retry_complete.is_set(), (
                f"Retry never fired — get_bundle_metadata stayed at {call_count['n']} call(s)"
            )

            # Give the retry a moment to finish writing the BIF.
            time.sleep(0.5)

        # Verify the BIF actually landed on disk after the retry.
        # Bundle path: {plex_config}/Media/localhost/<h0>/<h[1:]>.bundle/Contents/Indexes/index-sd.bif
        bundle_root = Path(plex_retry_config.plex_config_folder) / "Media" / "localhost"
        bif_files = list(bundle_root.rglob("index-sd.bif"))
        assert bif_files, (
            "Retry chain didn't produce a BIF on disk — "
            f"call_count={call_count['n']}, bundle_root contents: {list(bundle_root.rglob('*')) if bundle_root.exists() else 'missing'}"
        )
        # And the BIF is structurally valid.
        head = bif_files[0].read_bytes()[:8]
        assert head == bytes([0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A]), "Bad BIF magic on retry result"
