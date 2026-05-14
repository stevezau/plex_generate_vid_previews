"""Tests for cleanup_orphaned_outputs.

Two passes:
  1. Targeted webhook-driven (deletedFiles[]).
  2. Post-publish neighbor sweep (safety net).

Matrix coverage per .claude/rules/testing.md:
  * deleted_paths: present / absent / partially-malformed
  * folder shape: empty / one orphan / live + orphan / no video files (mount issue)
  * adapter mix: Jellyfin only / Emby only / both
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from media_preview_generator.processing.multi_server import cleanup_orphaned_outputs
from media_preview_generator.servers.base import ServerConfig, ServerType


def _touch(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _make_registry_with_servers(types: list[str]) -> MagicMock:
    """Build a stub registry whose ``configs()`` returns ``ServerConfig`` rows.

    ``types`` is a list of strings like ``["jellyfin", "emby"]``. Each
    becomes a single ServerConfig with default output shape — enough for
    ``_adapter_for_server`` to construct the right adapter.
    """
    type_map = {
        "jellyfin": ServerType.JELLYFIN,
        "emby": ServerType.EMBY,
        "plex": ServerType.PLEX,
    }
    configs = []
    for i, t in enumerate(types):
        st = type_map[t]
        output: dict = {}
        if st is ServerType.PLEX:
            output["plex_config_folder"] = "/dev/null"
        configs.append(
            ServerConfig(
                id=f"srv-{i}",
                type=st,
                name=f"{t}-{i}",
                enabled=True,
                url="http://localhost",
                auth={},
                output=output,
            )
        )
    registry = MagicMock()
    registry.configs.return_value = configs
    return registry


# ---------------------------------------------------------------------------
# Pass 1: targeted webhook-driven removal
# ---------------------------------------------------------------------------


class TestTargetedCleanup:
    def test_removes_jellyfin_trickplay_for_named_deleted_path(self, tmp_path, mock_config):
        """Radarr deletedFiles=[/path/Old.mkv] → Old.trickplay/ is removed."""
        # Sidecars from the OLD release (basename "Movie-OLD")
        old_trickplay = _mkdir(tmp_path / "Movie-OLD.trickplay")
        # The NEW file lives next to it (basename "Movie-NEW")
        _touch(tmp_path / "Movie-NEW.mkv")

        registry = _make_registry_with_servers(["jellyfin"])
        old_path = str(tmp_path / "Movie-OLD.mkv")  # no longer on disk
        new_canonical = str(tmp_path / "Movie-NEW.mkv")

        removed = cleanup_orphaned_outputs(
            new_canonical,
            deleted_paths=[old_path],
            registry=registry,
            config=mock_config,
        )

        assert old_trickplay in removed
        assert not old_trickplay.exists()

    def test_removes_emby_bif_and_meta_for_named_deleted_path(self, tmp_path, mock_config):
        old_bif = _touch(tmp_path / "Movie-OLD-320-10.bif")
        old_meta = _touch(tmp_path / "Movie-OLD-320-10.bif.meta")
        _touch(tmp_path / "Movie-NEW.mkv")
        new_bif = _touch(tmp_path / "Movie-NEW-320-10.bif")

        registry = _make_registry_with_servers(["emby"])
        removed = cleanup_orphaned_outputs(
            str(tmp_path / "Movie-NEW.mkv"),
            deleted_paths=[str(tmp_path / "Movie-OLD.mkv")],
            registry=registry,
            config=mock_config,
        )

        assert old_bif in removed
        assert old_meta in removed
        assert new_bif not in removed
        assert not old_bif.exists()
        assert not old_meta.exists()
        assert new_bif.exists()  # live sidecar protected

    def test_targeted_pass_works_across_folder_boundaries(self, tmp_path, mock_config):
        """OLD path is in folder A, NEW path in folder B — sweep cleans A."""
        folder_a = _mkdir(tmp_path / "season1")
        folder_b = _mkdir(tmp_path / "season2")
        old_trickplay = _mkdir(folder_a / "Show-S01E01-OLD.trickplay")
        _touch(folder_b / "Show-S01E01-NEW.mkv")

        registry = _make_registry_with_servers(["jellyfin"])
        removed = cleanup_orphaned_outputs(
            str(folder_b / "Show-S01E01-NEW.mkv"),
            deleted_paths=[str(folder_a / "Show-S01E01-OLD.mkv")],
            registry=registry,
            config=mock_config,
        )

        assert old_trickplay in removed
        assert not old_trickplay.exists()

    def test_targeted_pass_skips_unrelated_orphans(self, tmp_path, mock_config):
        """Only the named deleted basename is touched in pass 1."""
        named_orphan = _mkdir(tmp_path / "Movie-A.trickplay")
        unrelated_orphan = _mkdir(tmp_path / "Movie-B.trickplay")
        # Note: pass 2 (sweep) WOULD remove unrelated_orphan too, but we
        # need to test pass 1 in isolation. Drop a video file with the
        # same stem as the unrelated orphan so the sweep treats it as live.
        _touch(tmp_path / "Movie-B.mkv")

        registry = _make_registry_with_servers(["jellyfin"])
        removed = cleanup_orphaned_outputs(
            str(tmp_path / "Movie-B.mkv"),
            deleted_paths=[str(tmp_path / "Movie-A.mkv")],
            registry=registry,
            config=mock_config,
        )

        assert named_orphan in removed
        assert unrelated_orphan not in removed
        assert not named_orphan.exists()
        assert unrelated_orphan.exists()


# ---------------------------------------------------------------------------
# Pass 2: post-publish neighbor sweep
# ---------------------------------------------------------------------------


class TestNeighborSweep:
    def test_sweeps_orphans_when_no_deleted_paths_provided(self, tmp_path, mock_config):
        """Sweep runs even with deleted_paths=None — catches manual renames."""
        orphan = _mkdir(tmp_path / "Movie-OLD.trickplay")
        _touch(tmp_path / "Movie-NEW.mkv")

        registry = _make_registry_with_servers(["jellyfin"])
        removed = cleanup_orphaned_outputs(
            str(tmp_path / "Movie-NEW.mkv"),
            deleted_paths=None,
            registry=registry,
            config=mock_config,
        )

        assert orphan in removed
        assert not orphan.exists()

    def test_sweep_protects_live_artifacts(self, tmp_path, mock_config):
        live_trickplay = _mkdir(tmp_path / "Movie.trickplay")
        _touch(tmp_path / "Movie.mkv")

        registry = _make_registry_with_servers(["jellyfin"])
        removed = cleanup_orphaned_outputs(
            str(tmp_path / "Movie.mkv"),
            deleted_paths=None,
            registry=registry,
            config=mock_config,
        )

        assert removed == []
        assert live_trickplay.exists()

    def test_sweep_skipped_when_folder_has_no_video_files(self, tmp_path, mock_config):
        """Mount-offline guard: empty folder of video files → no deletes."""
        # Lots of orphans...
        orphan_a = _mkdir(tmp_path / "MovieA.trickplay")
        orphan_b = _touch(tmp_path / "MovieB-320-10.bif")
        # ...but no .mkv / .mp4 anywhere → mount likely offline.

        registry = _make_registry_with_servers(["jellyfin", "emby"])
        # canonical_path's folder is THIS folder; no live videos →
        # sweep refuses to delete anything.
        removed = cleanup_orphaned_outputs(
            str(tmp_path / "Movie.mkv"),  # canonical doesn't exist either
            deleted_paths=None,
            registry=registry,
            config=mock_config,
        )

        assert removed == []
        assert orphan_a.exists()
        assert orphan_b.exists()

    def test_sweep_with_jellyfin_and_emby_both_configured(self, tmp_path, mock_config):
        """Both adapters enumerate; both kinds of orphans get cleaned."""
        old_trickplay = _mkdir(tmp_path / "Movie-OLD.trickplay")
        old_bif = _touch(tmp_path / "Movie-OLD-320-10.bif")
        old_meta = _touch(tmp_path / "Movie-OLD-320-10.bif.meta")
        _touch(tmp_path / "Movie-NEW.mkv")

        registry = _make_registry_with_servers(["jellyfin", "emby"])
        removed = set(
            cleanup_orphaned_outputs(
                str(tmp_path / "Movie-NEW.mkv"),
                deleted_paths=None,
                registry=registry,
                config=mock_config,
            )
        )

        assert old_trickplay in removed
        assert old_bif in removed
        assert old_meta in removed


# ---------------------------------------------------------------------------
# Idempotency + safety
# ---------------------------------------------------------------------------


class TestCleanupIdempotency:
    def test_returns_empty_when_no_orphans(self, tmp_path, mock_config):
        _touch(tmp_path / "Movie.mkv")
        registry = _make_registry_with_servers(["jellyfin"])
        removed = cleanup_orphaned_outputs(
            str(tmp_path / "Movie.mkv"),
            deleted_paths=None,
            registry=registry,
            config=mock_config,
        )
        assert removed == []

    def test_targeted_pass_with_path_that_doesnt_exist(self, tmp_path, mock_config):
        """deleted_paths references a path whose folder was already cleaned →
        no error, no result."""
        _touch(tmp_path / "Movie.mkv")
        registry = _make_registry_with_servers(["jellyfin"])
        # No old artifacts on disk; targeted pass enumerates and finds
        # nothing → no exception, empty result.
        removed = cleanup_orphaned_outputs(
            str(tmp_path / "Movie.mkv"),
            deleted_paths=["/nonexistent/folder/Old.mkv"],
            registry=registry,
            config=mock_config,
        )
        assert removed == []

    def test_targeted_and_sweep_dont_double_remove(self, tmp_path, mock_config):
        """When both passes target the same orphan, it's reported only once."""
        old = _mkdir(tmp_path / "Movie-OLD.trickplay")
        _touch(tmp_path / "Movie-NEW.mkv")

        registry = _make_registry_with_servers(["jellyfin"])
        removed = cleanup_orphaned_outputs(
            str(tmp_path / "Movie-NEW.mkv"),
            deleted_paths=[str(tmp_path / "Movie-OLD.mkv")],
            registry=registry,
            config=mock_config,
        )

        # Targeted pass got it first; sweep saw it was already removed.
        assert removed.count(old) == 1
        assert not old.exists()

    def test_no_adapters_configured_returns_empty(self, tmp_path, mock_config):
        """Empty registry → nothing to enumerate → no-op."""
        registry = MagicMock()
        registry.configs.return_value = []
        _touch(tmp_path / "Movie.mkv")
        _mkdir(tmp_path / "Movie-OLD.trickplay")  # would be orphan if Jellyfin were configured

        removed = cleanup_orphaned_outputs(
            str(tmp_path / "Movie.mkv"),
            deleted_paths=None,
            registry=registry,
            config=mock_config,
        )

        assert removed == []

    def test_empty_string_in_deleted_paths_is_ignored(self, tmp_path, mock_config):
        """Defensive: empty / falsy entries don't crash the pass."""
        _touch(tmp_path / "Movie.mkv")
        registry = _make_registry_with_servers(["jellyfin"])
        removed = cleanup_orphaned_outputs(
            str(tmp_path / "Movie.mkv"),
            deleted_paths=["", str(tmp_path / "missing.mkv")],
            registry=registry,
            config=mock_config,
        )
        assert removed == []


# ---------------------------------------------------------------------------
# In-place upgrade safety (Radarr deletedFiles[] echoes the new path)
# ---------------------------------------------------------------------------


class TestInPlaceUpgradeSafety:
    """Regression: Radarr's ``Download`` webhook for an in-place upgrade
    (same filename overwritten with new content) lists the path of the
    OLD file in ``deletedFiles[]`` — but that path now hosts the NEW
    file. Pre-fix, cleanup happily deleted the new file's sidecars
    because the basename matched. The retry's regenerate eventually
    restored them, hiding the data loss in the slow path.

    Reproduced live 2026-05-09 with Gary (2026) — Radarr fired
    ``deletedFiles=[/data_16tb/.../Gary HDSWEB.mkv]`` for an in-place
    upgrade; cleanup deleted the live ``.trickplay/`` + ``.bif`` until
    the retry restored them.

    Two protections (defence in depth):
      1. ``os.path.exists(old_path)`` → skip (the "deletion" is a lie)
      2. ``Path(old_path).stem == Path(canonical_path).stem`` → skip
         (path-mapping edge case where the path differs but resolves
         to the same file).
    """

    def test_in_place_upgrade_same_path_preserves_live_artifacts(self, tmp_path, mock_config):
        """deletedFiles=[same path as canonical] → live sidecars preserved."""
        # Live artifacts after a successful publish.
        live_mkv = _touch(tmp_path / "Movie.mkv")
        live_trickplay = _mkdir(tmp_path / "Movie.trickplay")
        live_bif = _touch(tmp_path / "Movie-320-10.bif")

        registry = _make_registry_with_servers(["jellyfin", "emby"])
        removed = cleanup_orphaned_outputs(
            str(live_mkv),
            # Radarr's payload claims the same path was deleted (in-place upgrade).
            deleted_paths=[str(live_mkv)],
            registry=registry,
            config=mock_config,
        )

        assert removed == [], (
            "In-place upgrade MUST NOT delete the live file's sidecars "
            "even when Radarr's deletedFiles[] echoes the new path."
        )
        assert live_trickplay.exists()
        assert live_bif.exists()

    def test_in_place_upgrade_different_mount_same_basename(self, tmp_path, mock_config):
        """deletedFiles=[different mount path, same basename] → still preserved.

        Path-mapping edge case: Radarr might surface ``/data/Movies/X.mkv``
        while the canonical path is ``/data_16tb/Movies/X.mkv``. Both
        resolve to the same file via path mapping; the basename equality
        check protects against this even when ``os.path.exists`` for
        the foreign mount returns False.
        """
        live_mkv = _touch(tmp_path / "Movie.mkv")
        live_trickplay = _mkdir(tmp_path / "Movie.trickplay")

        registry = _make_registry_with_servers(["jellyfin"])
        # Different folder, same basename — simulates Radarr giving a
        # different mount path.
        foreign_mount_path = "/never_existed/Movies/Movie.mkv"
        removed = cleanup_orphaned_outputs(
            str(live_mkv),
            deleted_paths=[foreign_mount_path],
            registry=registry,
            config=mock_config,
        )

        assert removed == [], (
            "Same basename as canonical_path MUST trigger the safety "
            "skip even when the deleted_paths entry references a "
            "different mount."
        )
        assert live_trickplay.exists()

    def test_genuine_upgrade_with_different_basename_still_cleans(self, tmp_path, mock_config):
        """Sanity: when the upgrade ACTUALLY changed basenames (different
        release group), cleanup still removes the orphan. The safety
        guards above must not be so broad they break the happy path.
        """
        # Simulate a real upgrade: -OLD.mkv was replaced by -NEW.mkv.
        new_mkv = _touch(tmp_path / "Movie-NEW.mkv")
        old_orphan = _mkdir(tmp_path / "Movie-OLD.trickplay")
        # The OLD .mkv is gone (Radarr deleted it after import).

        registry = _make_registry_with_servers(["jellyfin"])
        removed = cleanup_orphaned_outputs(
            str(new_mkv),
            deleted_paths=[str(tmp_path / "Movie-OLD.mkv")],
            registry=registry,
            config=mock_config,
        )

        assert old_orphan in removed
        assert not old_orphan.exists()


# ---------------------------------------------------------------------------
# Integration: cleanup runs on the all-fresh fast path
# ---------------------------------------------------------------------------


class TestCleanupOnAllFreshFastPath:
    """Smoke-test regression: the all-fresh fast path in
    ``process_canonical_path`` (``All publishers' outputs already fresh —
    skipping FFmpeg``) MUST still invoke cleanup. Pre-fix the fast path
    returned without calling ``cleanup_orphaned_outputs``, so an upgrade
    webhook arriving for a file whose outputs were already on disk
    silently dropped the deletion signal — orphan ``.trickplay/`` /
    ``.bif`` sidecars from the old release lingered forever.

    Reproduced in production smoke test 2026-05-09 with the No Ordinary
    Heist (2026) Radarr upgrade payload. First dispatch ran the cleanup
    code path, second hit the all-fresh fast path and orphans survived
    until this fix landed.
    """

    def test_fast_path_invokes_cleanup_with_deleted_paths(self, tmp_path, mock_config):
        """End-to-end: process_canonical_path → all-fresh path → orphan removed.

        Builds a real ServerRegistry + on-disk artifacts so the all-fresh
        check actually fires. The fake orphan should be gone after dispatch
        even though FFmpeg never ran.
        """
        from media_preview_generator.processing.multi_server import process_canonical_path
        from media_preview_generator.servers import ServerRegistry

        media_dir = tmp_path / "Movie (2024)"
        media_dir.mkdir()
        live_mkv = _touch(media_dir / "Movie (2024) -NEW.mkv")
        # Live trickplay matches NEW basename — must NOT be removed.
        live_trickplay_dir = media_dir / "Movie (2024) -NEW.trickplay" / "320 - 10x10"
        live_trickplay_dir.mkdir(parents=True)
        sheet0 = _touch(live_trickplay_dir / "0.jpg", b"\xff\xd8\xff")
        # Stamp the journal so outputs_fresh_for_source returns True.
        from media_preview_generator.output.journal import write_meta

        write_meta([sheet0], str(live_mkv), publisher="jellyfin_trickplay")
        # Orphan from the OLD release — should be cleaned up.
        old_trickplay = _mkdir(media_dir / "Movie (2024) -OLD.trickplay")

        registry = ServerRegistry.from_settings(
            [
                {
                    "id": "jelly-1",
                    "type": "jellyfin",
                    "name": "JellyTest",
                    "enabled": True,
                    "url": "http://jelly:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "libraries": [
                        {
                            "id": "1",
                            "name": "Movies",
                            "remote_paths": [str(tmp_path)],
                            "enabled": True,
                        }
                    ],
                    "exclude_paths": [],
                    "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 10},
                }
            ],
        )

        # Stub trigger_refresh + resolve_remote_path so the fast path
        # doesn't try to actually hit Jellyfin over the wire.
        from unittest.mock import patch as _patch

        from media_preview_generator.servers.jellyfin import JellyfinServer

        with (
            _patch.object(JellyfinServer, "trigger_refresh") as refresh_mock,
            _patch.object(JellyfinServer, "resolve_remote_path_to_item_id", return_value=None),
        ):
            mock_config.working_tmp_folder = str(tmp_path / "tmp")
            process_canonical_path(
                canonical_path=str(live_mkv),
                registry=registry,
                config=mock_config,
                deleted_paths=[str(media_dir / "Movie (2024) -OLD.mkv")],
                schedule_retry_on_not_indexed=False,
            )

        # Orphan removed (the regression: pre-fix it survived).
        assert not old_trickplay.exists(), (
            "All-fresh fast path failed to run cleanup_orphaned_outputs — "
            "orphan trickplay dir from previous release survived the dispatch."
        )
        # Live artifact preserved.
        assert live_trickplay_dir.exists()
        # The deleted-path nudge fired on the fast path too —
        # boundary-kwargs assertion per .claude/rules/testing.md.
        assert refresh_mock.called, "trigger_refresh was never invoked on the fast path"
        deleted_calls = [c for c in refresh_mock.call_args_list if c.kwargs.get("deleted_paths")]
        assert deleted_calls, (
            "trigger_refresh was called but never with deleted_paths — "
            "Jellyfin/Emby never received UpdateType:Deleted for the old release."
        )
        assert deleted_calls[0].kwargs["deleted_paths"] == [str(media_dir / "Movie (2024) -OLD.mkv")]
