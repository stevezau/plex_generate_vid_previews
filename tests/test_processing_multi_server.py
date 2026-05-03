"""Tests for the multi-server processing entry point.

Verifies that:

- `process_canonical_path` resolves owning servers via the registry and
  fans out to each one's adapter,
- one FFmpeg pass feeds every publisher (frame extraction is mocked),
- per-publisher exceptions are captured into :class:`PublisherResult`
  rather than crashing the whole call,
- the "no owners" / "no frames" / "source missing" branches return
  the right :class:`MultiServerStatus`.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.processing.frame_cache import reset_frame_cache
from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    _adapter_for_server,
    process_canonical_path,
)
from media_preview_generator.servers import (
    Library,
    PlexServer,
    ServerConfig,
    ServerRegistry,
    ServerType,
)


@pytest.fixture(autouse=True)
def _reset_frame_cache_singleton():
    """Each test gets a fresh frame-cache singleton so the
    base_dir-conflict guard in :func:`get_frame_cache` doesn't fire
    across tests that use different ``tmp_path`` fixtures.
    """
    reset_frame_cache()
    yield
    reset_frame_cache()


def _populate_frames(directory: str | Path, count: int, *, real_images: bool = True) -> None:
    """Create ``count`` JPGs under ``directory``.

    When ``real_images`` is True (default) we write valid Pillow-encoded
    JPGs — the Jellyfin trickplay adapter passes them through Pillow,
    so its tests need decodable inputs. When False we write minimal
    JPEG-magic byte sequences which is enough for the BIF packer that
    only reads file lengths.
    """
    Path(directory).mkdir(parents=True, exist_ok=True)
    if real_images:
        from PIL import Image

        img = Image.new("RGB", (320, 180), (10, 20, 30))
        for i in range(count):
            img.save(Path(directory) / f"{i:05d}.jpg", "JPEG", quality=70)
    else:
        for i in range(count):
            (Path(directory) / f"{i:05d}.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)


def _seed_canonical_file(media_dir: Path, *, name: str = "Test (2024).mkv") -> Path:
    """Create a fake media file so ``os.path.isfile`` passes."""
    media_dir.mkdir(parents=True, exist_ok=True)
    media_file = media_dir / name
    media_file.write_bytes(b"placeholder")
    return media_file


def _server_config(
    *,
    server_id: str,
    server_type: ServerType,
    libraries: list[Library],
    output: dict | None = None,
    exclude_paths: list[dict] | None = None,
) -> dict:
    return {
        "id": server_id,
        "type": server_type.value,
        "name": server_id,
        "enabled": True,
        "url": "http://x",
        "auth": {"token": "t", "method": "api_key", "api_key": "k"},
        "libraries": [
            {
                "id": lib.id,
                "name": lib.name,
                "remote_paths": list(lib.remote_paths),
                "enabled": lib.enabled,
            }
            for lib in libraries
        ],
        "exclude_paths": exclude_paths or [],
        "output": output
        or {
            "adapter": {
                ServerType.PLEX: "plex_bundle",
                ServerType.EMBY: "emby_sidecar",
                ServerType.JELLYFIN: "jellyfin_trickplay",
            }[server_type],
            "plex_config_folder": "/cfg",
            "width": 320,
            "frame_interval": 10,
        },
    }


@pytest.fixture
def mock_config_for_processing(mock_config, tmp_path):
    mock_config.working_tmp_folder = str(tmp_path / "tmp")
    return mock_config


class TestNoOwners:
    def test_returns_no_owners_when_no_server_covers_path(self, mock_config_for_processing):
        registry = ServerRegistry()
        result = process_canonical_path(
            canonical_path="/data/movies/Foo.mkv",
            registry=registry,
            config=mock_config_for_processing,
        )
        assert result.status is MultiServerStatus.NO_OWNERS
        assert result.publishers == []


class TestPerServerExcludePaths:
    """Per-server exclude_paths filtering — one server skips, others publish.

    The dispatcher consults each owning server's ``exclude_paths`` list
    before adding it to the publishers fan-out. A user can have very
    different exclusion rules per server (skip /Trailers/ on Jellyfin
    only, etc.).
    """

    def test_excluded_server_is_filtered_out(self, mock_config_for_processing, tmp_path):
        # Two servers both own the path; only one excludes it.
        media_dir = tmp_path / "movies" / "Trailer (2024)"
        media_file = _seed_canonical_file(media_dir)

        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="emby-1",
                    server_type=ServerType.EMBY,
                    libraries=[Library(id="1", name="Movies", remote_paths=(str(tmp_path / "movies"),), enabled=True)],
                    exclude_paths=[{"value": str(media_file), "type": "path"}],
                ),
                _server_config(
                    server_id="jellyfin-1",
                    server_type=ServerType.JELLYFIN,
                    libraries=[Library(id="2", name="Movies", remote_paths=(str(tmp_path / "movies"),), enabled=True)],
                ),
            ],
        )

        def fake_generate_images(video_file, output_folder, *args, **kwargs):
            _populate_frames(output_folder, count=5)
            return (True, 5, "h264", 1.0, 30.0, None)

        with patch(
            "media_preview_generator.processing.multi_server.generate_images",
            side_effect=fake_generate_images,
        ):
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
            )
        # Emby was excluded; only Jellyfin should appear in the publishers list.
        published_server_ids = [p.server_id for p in result.publishers]
        assert "emby-1" not in published_server_ids
        assert "jellyfin-1" in published_server_ids

    def test_no_servers_remain_after_exclusion_returns_no_owners(self, mock_config_for_processing, tmp_path):
        media_file = tmp_path / "movies" / "OnlyExcluded.mkv"
        media_file.parent.mkdir(parents=True)
        media_file.write_bytes(b"fake")

        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="emby-1",
                    server_type=ServerType.EMBY,
                    libraries=[Library(id="1", name="Movies", remote_paths=(str(tmp_path / "movies"),), enabled=True)],
                    exclude_paths=[{"value": str(media_file), "type": "path"}],
                ),
            ],
        )
        result = process_canonical_path(
            canonical_path=str(media_file),
            registry=registry,
            config=mock_config_for_processing,
        )
        # Every owning server excluded the path → no publishers, NO_OWNERS.
        assert result.status is MultiServerStatus.NO_OWNERS


class TestSiblingMountProbe:
    """D35: when Plex's indexed path is stale (file moved to a sibling
    disk after a post-import script), probe the other configured local
    mounts before declaring the source missing.

    Reproduces job 1089f843: Plex returned /data_16tb3/Sports/X.mkv but
    the file was actually at /data_16tb/Sports/X.mkv. Without the probe,
    the dispatcher hit SKIPPED_FILE_NOT_FOUND and (with D33) scheduled
    a retry that would have failed identically every time."""

    def test_finds_file_at_sibling_mount_when_canonical_stale(self, mock_config_for_processing, tmp_path):
        # Create file at /data_16tb-equivalent (tmp_path / "live").
        live_dir = tmp_path / "live" / "Sports"
        live_dir.mkdir(parents=True)
        live_file = live_dir / "Wolves vs Sunderland.mkv"
        live_file.write_bytes(b"fake mkv")

        # Plex (stale) reports it at /data_16tb3-equivalent (tmp_path / "stale").
        stale_path = str(tmp_path / "stale" / "Sports" / "Wolves vs Sunderland.mkv")

        cfg_dict = _server_config(
            server_id="plex-1",
            server_type=ServerType.PLEX,
            libraries=[
                Library(
                    id="1",
                    name="Sports",
                    remote_paths=(str(tmp_path / "live"), str(tmp_path / "stale")),
                    enabled=True,
                )
            ],
        )
        cfg_dict["path_mappings"] = [
            {"plex_prefix": str(tmp_path / "live"), "local_prefix": str(tmp_path / "live")},
            {"plex_prefix": str(tmp_path / "stale"), "local_prefix": str(tmp_path / "stale")},
        ]
        registry = ServerRegistry.from_settings([cfg_dict])
        result = process_canonical_path(
            canonical_path=stale_path,
            registry=registry,
            config=mock_config_for_processing,
        )
        # Probe rebound canonical_path to the live mount; result is NOT
        # SKIPPED_FILE_NOT_FOUND. (May be other status depending on
        # what the rest of the pipeline does with the rebound path.)
        assert result.status is not MultiServerStatus.SKIPPED_FILE_NOT_FOUND, (
            "sibling-mount probe should have found the file at the live mount; "
            f"got status={result.status} message={result.message!r}"
        )

    def test_single_mount_falls_through_to_skipped(self, mock_config_for_processing, tmp_path):
        """No siblings to probe → behave exactly like before D35."""
        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="emby-1",
                    server_type=ServerType.EMBY,
                    libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)],
                )
            ],
        )
        result = process_canonical_path(
            canonical_path="/data/movies/missing.mkv",
            registry=registry,
            config=mock_config_for_processing,
        )
        assert result.status is MultiServerStatus.SKIPPED_FILE_NOT_FOUND


class TestSourceMissing:
    def test_returns_skipped_file_not_found_when_source_file_missing(self, mock_config_for_processing, tmp_path):
        """Source-missing → SKIPPED_FILE_NOT_FOUND, NOT FAILED.

        Why this matters (D33 regression): the webhook retry path in
        job_runner.py triggers on outcome=='skipped_file_not_found'. If
        we return MultiServerStatus.FAILED instead, the file maps to
        outcome 'failed' and the retry never fires — even though
        "file missing on disk" right after a webhook is the EXACT
        case retry was built for (Sonarr/Radarr fire at download-start
        in many setups, so the file is mid-copy when we look).

        The user-flagged reproducer: job 1089f843 had two webhook paths,
        both resolved by Plex, both failed with "Source video file is
        missing on disk", and zero retries fired. With this status
        change the retry path engages instead.
        """
        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="emby-1",
                    server_type=ServerType.EMBY,
                    libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)],
                )
            ],
        )
        result = process_canonical_path(
            canonical_path="/data/movies/missing.mkv",
            registry=registry,
            config=mock_config_for_processing,
        )
        assert result.status is MultiServerStatus.SKIPPED_FILE_NOT_FOUND, (
            f"got {result.status} — webhook retry only fires on SKIPPED_FILE_NOT_FOUND; "
            "FAILED would silently disable retry for the most-common-retry case (mid-copy webhooks)"
        )
        assert "not found" in result.message.lower()


class TestSinglePublisher:
    def test_emby_publisher_runs_one_ffmpeg_pass(self, mock_config_for_processing, tmp_path):
        media_dir = tmp_path / "data" / "movies" / "Test (2024)"
        media_file = _seed_canonical_file(media_dir)

        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="emby-1",
                    server_type=ServerType.EMBY,
                    libraries=[
                        Library(
                            id="1",
                            name="Movies",
                            remote_paths=(str(tmp_path / "data" / "movies"),),
                            enabled=True,
                        )
                    ],
                    output={"adapter": "emby_sidecar", "width": 320, "frame_interval": 10},
                )
            ],
        )

        # Mock FFmpeg frame generation: drop synthetic frames into the tmp dir.
        def fake_generate_images(video_file, output_folder, *args, **kwargs):
            _populate_frames(output_folder, count=5)
            return (True, 5, "h264", 1.0, 30.0, None)

        with patch(
            "media_preview_generator.processing.multi_server.generate_images",
            side_effect=fake_generate_images,
        ) as gen:
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
            )

        assert result.status is MultiServerStatus.PUBLISHED
        assert result.frame_count == 5
        assert len(result.publishers) == 1
        assert result.publishers[0].status is PublisherStatus.PUBLISHED
        # Exactly one FFmpeg pass — the cornerstone of the multi-server design.
        assert gen.call_count == 1

        # Sidecar BIF appeared next to the media.
        sidecar = media_dir / "Test (2024)-320-10.bif"
        assert sidecar.exists()


class TestMultiPublisherFanOut:
    def test_one_pass_feeds_emby_and_jellyfin(self, mock_config_for_processing, tmp_path):
        media_dir = tmp_path / "data" / "movies" / "Test (2024)"
        media_file = _seed_canonical_file(media_dir)
        media_root = str(tmp_path / "data" / "movies")

        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="emby-1",
                    server_type=ServerType.EMBY,
                    libraries=[Library(id="1", name="Movies", remote_paths=(media_root,), enabled=True)],
                    output={"adapter": "emby_sidecar", "width": 320, "frame_interval": 10},
                ),
                _server_config(
                    server_id="jelly-1",
                    server_type=ServerType.JELLYFIN,
                    libraries=[Library(id="9", name="Movies", remote_paths=(media_root,), enabled=True)],
                    output={"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 10},
                ),
            ],
        )

        def fake_generate_images(video_file, output_folder, *args, **kwargs):
            _populate_frames(output_folder, count=12)
            return (True, 12, "h264", 1.0, 30.0, None)

        with patch(
            "media_preview_generator.processing.multi_server.generate_images",
            side_effect=fake_generate_images,
        ) as gen:
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
                item_id_by_server={"jelly-1": "jf-item-id"},
            )

        # One pass, two publishers, both succeed.
        assert gen.call_count == 1
        assert result.status is MultiServerStatus.PUBLISHED
        assert len(result.publishers) == 2
        statuses = {p.server_id: p.status for p in result.publishers}
        assert statuses["emby-1"] is PublisherStatus.PUBLISHED
        assert statuses["jelly-1"] is PublisherStatus.PUBLISHED

        # Both formats landed on disk in the layouts each vendor expects.
        assert (media_dir / "Test (2024)-320-10.bif").exists()
        assert (media_dir / "Test (2024).trickplay" / "320 - 10x10" / "0.jpg").exists()


class TestCrossServerBifReuse:
    """D34 — when one publisher already has a fresh BIF for an unchanged
    source file, unpack it and feed the frames to a sibling publisher
    instead of running FFmpeg again. The user's ask: "if all servers
    selected and one has BIF files we reuse that for the others
    regardless of when it was created, as long as the files are the same."
    """

    @staticmethod
    def _write_real_bif(path: Path, frames: list[bytes]) -> None:
        """Write a minimally valid BIF that round-trips through unpack_bif_to_jpegs."""
        import array
        import struct

        magic = [0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A]
        count = len(frames)
        with open(path, "wb") as f:
            array.array("B", magic).tofile(f)
            f.write(struct.pack("<I", 0))  # version
            f.write(struct.pack("<I", count))
            f.write(struct.pack("<I", 1000))  # interval ms
            array.array("B", [0x00] * 44).tofile(f)
            table_size = 8 + (8 * count)
            image_offset = 64 + table_size
            for i, frame in enumerate(frames):
                f.write(struct.pack("<I", i))
                f.write(struct.pack("<I", image_offset))
                image_offset += len(frame)
            f.write(struct.pack("<I", 0xFFFFFFFF))
            f.write(struct.pack("<I", image_offset))
            for frame in frames:
                f.write(frame)

    def test_emby_existing_bif_feeds_jellyfin_without_running_ffmpeg(self, mock_config_for_processing, tmp_path):
        """Two publishers: Emby has a fresh BIF, Jellyfin doesn't have a
        manifest. The BIF reuse helper must unpack Emby's BIF for the
        Jellyfin publish so generate_images is NEVER called.
        """
        media_dir = tmp_path / "data" / "movies" / "Reuse (2024)"
        media_file = _seed_canonical_file(media_dir, name="Reuse (2024).mkv")
        media_root = str(tmp_path / "data" / "movies")

        # Pre-existing Emby sidecar BIF — naming matches the Emby adapter's
        # convention "{stem}-{width}-{frame_interval}.bif". Frames are
        # real Pillow-encoded JPEGs because the downstream Jellyfin
        # trickplay adapter pipes the unpacked frames through Pillow;
        # synthetic "\xff\xd8\xff…" bytes have the JPEG magic but won't
        # decode and trip the trickplay publish step.
        existing_bif = media_dir / "Reuse (2024)-320-10.bif"
        from io import BytesIO

        from PIL import Image

        real_frames: list[bytes] = []
        for _ in range(6):
            buf = BytesIO()
            Image.new("RGB", (320, 180), (10, 20, 30)).save(buf, "JPEG", quality=70)
            real_frames.append(buf.getvalue())
        self._write_real_bif(existing_bif, real_frames)
        # Force the source mtime to NOT be newer than the BIF; otherwise
        # outputs_fresh_for_source can return False on systems where
        # _seed_canonical_file's touch happens after our BIF write.
        bif_mtime = existing_bif.stat().st_mtime
        os.utime(media_file, (bif_mtime - 1, bif_mtime - 1))

        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="emby-1",
                    server_type=ServerType.EMBY,
                    libraries=[Library(id="1", name="Movies", remote_paths=(media_root,), enabled=True)],
                    output={"adapter": "emby_sidecar", "width": 320, "frame_interval": 10},
                ),
                _server_config(
                    server_id="jelly-1",
                    server_type=ServerType.JELLYFIN,
                    libraries=[Library(id="9", name="Movies", remote_paths=(media_root,), enabled=True)],
                    output={"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 10},
                ),
            ],
        )

        with patch(
            "media_preview_generator.processing.multi_server.generate_images",
        ) as gen:
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
                item_id_by_server={"jelly-1": "jf-item-id"},
            )

        # The headline assertion: FFmpeg never ran. All frames came from
        # unpacking Emby's pre-existing BIF.
        assert gen.call_count == 0, (
            f"BIF reuse must skip FFmpeg entirely when a sibling publisher already has a fresh BIF — "
            f"generate_images was called {gen.call_count} time(s)"
        )
        # And the Jellyfin trickplay published successfully off the
        # reused frames.
        assert result.status is MultiServerStatus.PUBLISHED
        statuses = {p.server_id: p.status for p in result.publishers}
        assert statuses["jelly-1"] is PublisherStatus.PUBLISHED
        # Emby's existing output is still SKIPPED_OUTPUT_EXISTS — its
        # BIF was already on disk, so the publisher takes the skip path
        # like any other repeat publish.
        assert statuses["emby-1"] is PublisherStatus.SKIPPED_OUTPUT_EXISTS

    def test_single_publisher_does_not_attempt_bif_reuse(self, mock_config_for_processing, tmp_path):
        """With only one owning publisher there's nobody to share BIF
        with. The all_fresh short-circuit handles "BIF exists, fresh"
        already; the cross-server reuse path must stay out of the way."""
        media_dir = tmp_path / "data" / "movies"
        media_file = _seed_canonical_file(media_dir, name="Solo (2024).mkv")
        media_root = str(media_dir)

        # NO existing BIF — generate_images MUST run (no reuse possible).
        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="emby-1",
                    server_type=ServerType.EMBY,
                    libraries=[Library(id="1", name="Movies", remote_paths=(media_root,), enabled=True)],
                    output={"adapter": "emby_sidecar", "width": 320, "frame_interval": 10},
                ),
            ],
        )

        def fake_generate_images(video_file, output_folder, *args, **kwargs):
            _populate_frames(output_folder, count=4)
            return (True, 4, "h264", 1.0, 30.0, None)

        with patch(
            "media_preview_generator.processing.multi_server.generate_images",
            side_effect=fake_generate_images,
        ) as gen:
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
            )

        assert gen.call_count == 1
        assert result.publishers[0].status is PublisherStatus.PUBLISHED


class TestPartialFailureIsolation:
    def test_jellyfin_missing_item_id_does_not_block_emby(self, mock_config_for_processing, tmp_path):
        # When Jellyfin's reverse-lookup can't resolve the canonical path
        # to an item_id (file not in its library yet), the dispatcher
        # short-circuits that publisher to SKIPPED_NOT_IN_LIBRARY and
        # the sibling Emby publisher proceeds normally. Previously this
        # branch was reported as FAILED with a "publish-time bookkeeping"
        # ValueError — see the dedicated SKIPPED_NOT_IN_LIBRARY tests in
        # TestNotInLibraryRoutesToSkip below for the user-visible message.
        media_dir = tmp_path / "data" / "movies" / "Test"
        media_file = _seed_canonical_file(media_dir)
        media_root = str(tmp_path / "data" / "movies")

        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="emby-1",
                    server_type=ServerType.EMBY,
                    libraries=[Library(id="1", name="Movies", remote_paths=(media_root,), enabled=True)],
                ),
                _server_config(
                    server_id="jelly-1",
                    server_type=ServerType.JELLYFIN,
                    libraries=[Library(id="9", name="Movies", remote_paths=(media_root,), enabled=True)],
                    output={"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 10},
                ),
            ],
        )

        def fake_generate_images(video_file, output_folder, *args, **kwargs):
            _populate_frames(output_folder, count=3)
            return (True, 3, "h264", 1.0, 30.0, None)

        with patch(
            "media_preview_generator.processing.multi_server.generate_images",
            side_effect=fake_generate_images,
        ):
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
                # No Jellyfin item id supplied -> compute_output_paths
                # would have raised; now short-circuited to SKIPPED.
                # Suppress the retry timer so a 30s-later retry doesn't
                # fire after pytest tears down loguru sinks.
                schedule_retry_on_not_indexed=False,
            )

        assert result.status is MultiServerStatus.PUBLISHED  # at least one succeeded
        statuses = {p.server_id: p.status for p in result.publishers}
        assert statuses["emby-1"] is PublisherStatus.PUBLISHED
        assert statuses["jelly-1"] is PublisherStatus.SKIPPED_NOT_IN_LIBRARY


class TestNotYetIndexedRoutesToSkip:
    def test_plex_returns_skipped_not_indexed_when_hash_missing(
        self, mock_config_for_processing, tmp_path, mock_config
    ):
        media_dir = tmp_path / "data" / "movies"
        media_file = _seed_canonical_file(media_dir)

        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="plex-1",
                    server_type=ServerType.PLEX,
                    libraries=[
                        Library(
                            id="1",
                            name="Movies",
                            remote_paths=(str(media_dir),),
                            enabled=True,
                        )
                    ],
                    output={
                        "adapter": "plex_bundle",
                        "plex_config_folder": str(tmp_path / "plex"),
                        "frame_interval": 10,
                    },
                )
            ],
            legacy_config=mock_config,
        )

        # D31-aware: stub the underlying plex.query (NOT get_bundle_metadata)
        # so the URL-construction layer actually runs. Mocking get_bundle_metadata
        # directly was the test pattern that hid D31 — every Sonarr/Radarr → Plex
        # webhook silently malformed the /tree URL and got 404'd. By mocking one
        # layer deeper we exercise the bare-id normalisation + URL builder.
        from xml.etree import ElementTree as ET

        plex_query_calls: list[str] = []

        def fake_plex_query(url):
            plex_query_calls.append(url)
            # Return XML with NO MediaPart hash — same end-state as "not indexed"
            # but proves get_bundle_metadata's URL was correctly formed.
            return ET.fromstring("<MediaContainer></MediaContainer>")

        def install_fake_plex(server_self):
            mock_plex = MagicMock()
            mock_plex.query = fake_plex_query
            server_self._plex = mock_plex
            return mock_plex

        with patch.object(PlexServer, "_connect", autospec=True, side_effect=install_fake_plex):

            def fake_generate_images(video_file, output_folder, *args, **kwargs):
                _populate_frames(output_folder, count=3)
                return (True, 3, "h264", 1.0, 30.0, None)

            with patch(
                "media_preview_generator.processing.multi_server.generate_images",
                side_effect=fake_generate_images,
            ):
                result = process_canonical_path(
                    canonical_path=str(media_file),
                    registry=registry,
                    config=mock_config_for_processing,
                    item_id_by_server={"plex-1": "42"},
                    # Don't schedule a real retry timer — pytest tears down
                    # loguru sinks after the test, and a 30s-later retry
                    # firing after teardown floods CI with
                    # "ValueError: I/O operation on closed file".
                    schedule_retry_on_not_indexed=False,
                )

        # Single skipped publisher — overall status is the dedicated
        # SKIPPED_NOT_INDEXED (D13). Distinct from generic SKIPPED so
        # the worker can map to ProcessingResult.SKIPPED_NOT_INDEXED and
        # the file outcome chip matches the per-server pill ("Not
        # Indexed Yet" everywhere) instead of falsely reading "Already
        # Existed" — which used to confuse users into thinking the BIF
        # was on disk when in fact the server was still scanning.
        assert len(result.publishers) == 1
        assert result.publishers[0].status is PublisherStatus.SKIPPED_NOT_INDEXED
        from media_preview_generator.processing.multi_server import MultiServerStatus

        assert result.status is MultiServerStatus.SKIPPED_NOT_INDEXED
        # D16 — friendly user-facing message; no "publisher" jargon, no
        # misleading "0 of 1 succeeded" wording.
        assert "Waiting for 1 server" in result.message
        assert "publisher" not in result.message.lower()
        # D31 — confirm every URL we hit Plex with had the correct, single-prefix
        # shape. Without this, a regression that doubled the prefix would still
        # produce an empty MediaPart list and this test would silently pass.
        # (The freshness pre-check + publisher path both query, hence multiple calls.)
        assert plex_query_calls, "plex.query was never called — adapter never reached Plex"
        for url in plex_query_calls:
            assert url == "/library/metadata/42/tree", (
                f"plex.query called with {url!r} — D31 regression "
                "(doubled /library/metadata/ prefix) would slip past this test."
            )


class TestNotInLibraryRoutesToSkip:
    """When ``resolve_remote_path_to_item_id`` returns None for an
    adapter that needs an item id (Jellyfin trickplay, Plex bundle),
    the publisher must report SKIPPED_NOT_IN_LIBRARY with a friendly
    message — NOT a confusing FAILED with the "publish-time bookkeeping"
    ValueError. Reproduces job b350d2ac where the user's Jellyfin had a
    different release of the same episode on a different drive than the
    canonical path, so the basename match returned None and every
    publish attempt was reported as a hard failure.
    """

    def test_jellyfin_returns_skipped_not_in_library_when_item_id_unresolvable(
        self, mock_config_for_processing, tmp_path
    ):
        media_dir = tmp_path / "data" / "movies"
        media_file = _seed_canonical_file(media_dir)

        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="jellyfin-1",
                    server_type=ServerType.JELLYFIN,
                    libraries=[
                        Library(
                            id="1",
                            name="Movies",
                            remote_paths=(str(media_dir),),
                            enabled=True,
                        )
                    ],
                    output={"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 10},
                )
            ],
        )

        # Stub the reverse-lookup to return None — emulates "Jellyfin's
        # library doesn't contain this exact file" (e.g. user has the
        # canonical release on /data_16tb2 but Jellyfin only indexed
        # the version on /data_16tb).
        from media_preview_generator.servers._embyish import EmbyApiClient

        # Track scan-nudge calls so we assert the publisher requested
        # a Jellyfin /Library/Refresh on the not-in-library branch.
        from media_preview_generator.servers.jellyfin import JellyfinServer

        scan_nudges: list[tuple[str | None, str | None]] = []

        def fake_trigger_refresh(self, *, item_id, remote_path):
            scan_nudges.append((item_id, remote_path))

        def fake_generate_images(video_file, output_folder, *args, **kwargs):
            _populate_frames(output_folder, count=3)
            return (True, 3, "h264", 1.0, 30.0, None)

        with (
            patch.object(EmbyApiClient, "resolve_remote_path_to_item_id", return_value=None),
            patch.object(JellyfinServer, "trigger_refresh", autospec=True, side_effect=fake_trigger_refresh),
            patch(
                "media_preview_generator.processing.multi_server.generate_images",
                side_effect=fake_generate_images,
            ),
        ):
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
                # Don't schedule a real retry timer — pytest tears down
                # loguru sinks after the test, and a 30s-later retry
                # firing after teardown floods CI with
                # "ValueError: I/O operation on closed file".
                schedule_retry_on_not_indexed=False,
            )

        # The publisher row is SKIPPED_NOT_IN_LIBRARY, NOT FAILED. This is
        # the bug the user hit on job b350d2ac — every Jellyfin publish for
        # a file Jellyfin didn't index was logged as failed=1 with a
        # cryptic "publish-time bookkeeping" message.
        assert len(result.publishers) == 1
        assert result.publishers[0].status is PublisherStatus.SKIPPED_NOT_IN_LIBRARY
        assert "library" in result.publishers[0].message.lower()
        # No mention of "publish-time bookkeeping" or "ValueError" — the
        # whole point of this branch is a clean user-facing message.
        assert "bookkeeping" not in result.publishers[0].message.lower()
        # Aggregate status collapses into SKIPPED_NOT_INDEXED so the file
        # outcome chip and the per-server pill match (the worker maps both
        # to the same Worker.outcome bucket — see D13).
        assert result.status is MultiServerStatus.SKIPPED_NOT_INDEXED
        # Scan was nudged for the not-in-library publisher so the next
        # retry has a fighting chance of finding the item.
        assert scan_nudges, "trigger_refresh was never called for not-in-library publisher"
        assert scan_nudges[0][0] is None  # item_id=None → fallback /Library/Refresh path
        assert scan_nudges[0][1] == str(media_file)


class TestSkipIfExists:
    def test_skips_publisher_when_output_already_present(self, mock_config_for_processing, tmp_path):
        media_dir = tmp_path / "data" / "movies"
        media_file = _seed_canonical_file(media_dir)
        existing_sidecar = media_dir / "Test (2024)-320-10.bif"
        existing_sidecar.write_bytes(b"already here")

        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="emby-1",
                    server_type=ServerType.EMBY,
                    libraries=[Library(id="1", name="Movies", remote_paths=(str(media_dir),), enabled=True)],
                )
            ],
        )

        def fake_generate_images(video_file, output_folder, *args, **kwargs):
            _populate_frames(output_folder, count=3)
            return (True, 3, "h264", 1.0, 30.0, None)

        with patch(
            "media_preview_generator.processing.multi_server.generate_images",
            side_effect=fake_generate_images,
        ):
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
            )

        assert result.publishers[0].status is PublisherStatus.SKIPPED_OUTPUT_EXISTS
        # Existing file untouched — content unchanged.
        assert existing_sidecar.read_bytes() == b"already here"
        # Frame provenance: the all-fresh short-circuit means FFmpeg never
        # ran for this publisher. The badge in the Job UI relies on this
        # being "output_existed" rather than the default "extracted".
        assert result.publishers[0].frame_source == "output_existed"

    def test_regenerate_overrides_skip(self, mock_config_for_processing, tmp_path):
        media_dir = tmp_path / "data" / "movies"
        media_file = _seed_canonical_file(media_dir)
        existing_sidecar = media_dir / "Test (2024)-320-10.bif"
        existing_sidecar.write_bytes(b"placeholder")

        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="emby-1",
                    server_type=ServerType.EMBY,
                    libraries=[Library(id="1", name="Movies", remote_paths=(str(media_dir),), enabled=True)],
                )
            ],
        )

        def fake_generate_images(video_file, output_folder, *args, **kwargs):
            _populate_frames(output_folder, count=3)
            return (True, 3, "h264", 1.0, 30.0, None)

        with patch(
            "media_preview_generator.processing.multi_server.generate_images",
            side_effect=fake_generate_images,
        ):
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
                regenerate=True,
            )

        assert result.publishers[0].status is PublisherStatus.PUBLISHED


class TestPublisherFailureModes:
    """Disk-full / EACCES scenarios — verify graceful PublisherStatus.FAILED."""

    def test_permission_denied_during_publish_returns_failed(self, mock_config_for_processing, tmp_path):
        """When the adapter's ``publish`` raises PermissionError, ``_publish_one``
        catches it (PermissionError is OSError) and reports FAILED.

        Without graceful handling the worker pool would die with an
        uncaught exception. We simulate by stubbing the EmbyBifAdapter's
        ``publish`` method to raise.
        """
        media_dir = tmp_path / "data" / "movies"
        media_file = _seed_canonical_file(media_dir)

        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="emby-1",
                    server_type=ServerType.EMBY,
                    libraries=[Library(id="1", name="Movies", remote_paths=(str(media_dir),), enabled=True)],
                )
            ],
        )

        def fake_generate_images(video_file, output_folder, *args, **kwargs):
            _populate_frames(output_folder, count=3)
            return (True, 3, "h264", 1.0, 30.0, None)

        # Patch the underlying generate_bif call so the publish path
        # raises PermissionError. The adapter wraps generate_bif which
        # writes the .bif file; that's where EACCES would happen in
        # the real world.
        with (
            patch(
                "media_preview_generator.processing.multi_server.generate_images",
                side_effect=fake_generate_images,
            ),
            patch(
                "media_preview_generator.processing.generator.generate_bif",
                side_effect=PermissionError(13, "Permission denied", "/data/movies/Test (2024)-320-10.bif"),
            ),
        ):
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
            )

        # Aggregate status is FAILED because every publisher failed.
        assert result.status is MultiServerStatus.FAILED
        # The publisher row is FAILED, not PUBLISHED.
        assert len(result.publishers) == 1
        assert result.publishers[0].status is PublisherStatus.FAILED
        # The error message surfaces the PermissionError so the user
        # can diagnose. Loose match — PermissionError stringifies as
        # "[Errno 13] Permission denied: ..." on POSIX.
        assert "Permission" in result.publishers[0].message or "denied" in result.publishers[0].message

    def test_disk_full_during_publish_returns_failed(self, mock_config_for_processing, tmp_path):
        """ENOSPC (OSError errno 28) also routes to FAILED, not crash."""
        media_dir = tmp_path / "data" / "movies"
        media_file = _seed_canonical_file(media_dir)

        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="emby-1",
                    server_type=ServerType.EMBY,
                    libraries=[Library(id="1", name="Movies", remote_paths=(str(media_dir),), enabled=True)],
                )
            ],
        )

        def fake_generate_images(video_file, output_folder, *args, **kwargs):
            _populate_frames(output_folder, count=3)
            return (True, 3, "h264", 1.0, 30.0, None)

        with (
            patch(
                "media_preview_generator.processing.multi_server.generate_images",
                side_effect=fake_generate_images,
            ),
            patch(
                "media_preview_generator.processing.generator.generate_bif",
                side_effect=OSError(28, "No space left on device", "/data/movies/Test.bif"),
            ),
        ):
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
            )

        assert result.status is MultiServerStatus.FAILED
        assert result.publishers[0].status is PublisherStatus.FAILED
        assert "No space" in result.publishers[0].message or "28" in result.publishers[0].message

    def test_compute_output_paths_oserror_returns_failed(self, mock_config_for_processing, tmp_path):
        """An OSError raised during ``compute_output_paths`` (e.g. by a
        Plex bundle adapter unable to query the API for the bundle hash)
        also produces FAILED, not an unhandled exception."""
        media_dir = tmp_path / "data" / "movies"
        media_file = _seed_canonical_file(media_dir)

        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="emby-1",
                    server_type=ServerType.EMBY,
                    libraries=[Library(id="1", name="Movies", remote_paths=(str(media_dir),), enabled=True)],
                )
            ],
        )

        def fake_generate_images(video_file, output_folder, *args, **kwargs):
            _populate_frames(output_folder, count=3)
            return (True, 3, "h264", 1.0, 30.0, None)

        from media_preview_generator.output.emby_sidecar import EmbyBifAdapter

        # Patching the instance method by class works because the
        # adapter is constructed by _adapter_for_server every dispatch.
        with (
            patch(
                "media_preview_generator.processing.multi_server.generate_images",
                side_effect=fake_generate_images,
            ),
            patch.object(
                EmbyBifAdapter,
                "compute_output_paths",
                side_effect=OSError("EIO simulated"),
            ),
        ):
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
            )

        assert result.status is MultiServerStatus.FAILED
        assert result.publishers[0].status is PublisherStatus.FAILED


class TestNoFrames:
    def test_returns_no_frames_when_ffmpeg_produces_zero(self, mock_config_for_processing, tmp_path):
        media_dir = tmp_path / "data" / "movies"
        media_file = _seed_canonical_file(media_dir)

        registry = ServerRegistry.from_settings(
            [
                _server_config(
                    server_id="emby-1",
                    server_type=ServerType.EMBY,
                    libraries=[Library(id="1", name="Movies", remote_paths=(str(media_dir),), enabled=True)],
                )
            ],
        )

        def fake_generate_images(*args, **kwargs):
            return (True, 0, "h264", 0.5, 30.0, "no frames")

        with patch(
            "media_preview_generator.processing.multi_server.generate_images",
            side_effect=fake_generate_images,
        ):
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
            )

        assert result.status is MultiServerStatus.NO_FRAMES


class TestAdapterFactory:
    def test_picks_default_per_server_type(self):
        plex_cfg = ServerConfig(
            id="p",
            type=ServerType.PLEX,
            name="P",
            enabled=True,
            url="x",
            auth={},
            output={"plex_config_folder": "/cfg"},
        )
        adapter = _adapter_for_server(plex_cfg)
        assert adapter is not None
        assert adapter.name == "plex_bundle"

    def test_unknown_adapter_returns_none(self):
        cfg = ServerConfig(
            id="x",
            type=ServerType.PLEX,
            name="x",
            enabled=True,
            url="x",
            auth={},
            output={"adapter": "completely_made_up"},
        )
        assert _adapter_for_server(cfg) is None

    def test_plex_without_config_folder_returns_none(self):
        cfg = ServerConfig(
            id="p",
            type=ServerType.PLEX,
            name="P",
            enabled=True,
            url="x",
            auth={},
            output={"adapter": "plex_bundle"},  # missing plex_config_folder
        )
        assert _adapter_for_server(cfg) is None


class TestSummariseResults:
    """D16 — friendly per-result message text used as the file's Details column."""

    def _result(self, status, msg=""):
        from media_preview_generator.processing.multi_server import PublisherResult

        return PublisherResult(server_id="s", server_name="S", adapter_name="a", status=status, message=msg)

    def test_published_one_server_says_published_to_1(self):
        from media_preview_generator.processing.multi_server import _summarise_results

        results = [self._result(PublisherStatus.PUBLISHED)]
        assert _summarise_results(results, MultiServerStatus.PUBLISHED) == "Published to 1 server"

    def test_published_two_servers_uses_plural(self):
        from media_preview_generator.processing.multi_server import _summarise_results

        results = [self._result(PublisherStatus.PUBLISHED), self._result(PublisherStatus.PUBLISHED)]
        assert _summarise_results(results, MultiServerStatus.PUBLISHED) == "Published to 2 servers"

    def test_partial_published_shows_n_of_m(self):
        from media_preview_generator.processing.multi_server import _summarise_results

        results = [self._result(PublisherStatus.PUBLISHED), self._result(PublisherStatus.FAILED)]
        assert _summarise_results(results, MultiServerStatus.PUBLISHED) == "Published to 1 of 2 servers"

    def test_skipped_outputs_existed_uses_friendly_phrase(self):
        from media_preview_generator.processing.multi_server import _summarise_results

        results = [self._result(PublisherStatus.SKIPPED_OUTPUT_EXISTS)]
        # The old wording was "0 of 1 publisher(s) succeeded" which read
        # as failure to users — they wanted the message to match the
        # outcome chip instead.
        assert _summarise_results(results, MultiServerStatus.SKIPPED) == "Already up to date on 1 server"

    def test_skipped_not_indexed_phrasing(self):
        """User-facing message must point at media-server analysis (not us)
        and avoid the misleading "indexing" verb (Plex DOES know the file
        exists; it just hasn't completed deep media analysis yet)."""
        from media_preview_generator.processing.multi_server import _summarise_results

        results = [self._result(PublisherStatus.SKIPPED_NOT_INDEXED)]
        msg = _summarise_results(results, MultiServerStatus.SKIPPED_NOT_INDEXED)
        assert msg == "Waiting for 1 server to scan / analyse the file"
        assert "publisher" not in msg.lower()

    def test_no_publisher_jargon_in_any_branch(self):
        """User-facing message must never use the internal 'publisher' term."""
        from media_preview_generator.processing.multi_server import _summarise_results

        for ms_status, pub_status in [
            (MultiServerStatus.PUBLISHED, PublisherStatus.PUBLISHED),
            (MultiServerStatus.SKIPPED, PublisherStatus.SKIPPED_OUTPUT_EXISTS),
            (MultiServerStatus.SKIPPED_NOT_INDEXED, PublisherStatus.SKIPPED_NOT_INDEXED),
            (MultiServerStatus.FAILED, PublisherStatus.FAILED),
        ]:
            msg = _summarise_results([self._result(pub_status)], ms_status)
            assert "publisher" not in msg.lower(), f"jargon leaked for {ms_status}: {msg!r}"
