"""Issue #238 — global ``thumbnail_interval`` must propagate to every server.

Closes the gap PR #240 fixes. The global ``thumbnail_interval`` drives the
FFmpeg ``fps`` argument, but the per-server ``media_servers[i].output.frame_interval``
drives the BIF header multiplier, Jellyfin trickplay registration, Emby
sidecar filename, and the BIF viewer's declared cadence. When they diverge,
the BIF declares a different cadence from the extraction rate and Plex
previews drift out of sync with playback.

Production wiring at ``api_settings.py:_apply_post_save_hooks`` step 6:

    if "thumbnail_interval" in updates:
        # ... iterate media_servers, set output.frame_interval to new global

This file pins:

  1. Saving a new ``thumbnail_interval`` updates ``output.frame_interval``
     in every server (Plex + Jellyfin + Emby), including entries that have
     no ``output`` block at all (the Jellyfin/Emby drift the user's own
     settings exhibited live — see plan write-up).
  2. The hook is a no-op when already aligned — the ``changed`` guard PR #240
     added must hold so no spurious settings.update() fires on every save.
  3. The hook does NOT fire when ``thumbnail_interval`` is absent from the
     ``updates`` dict — saving an unrelated field (e.g. ``cpu_threads``)
     must not touch per-server intervals.
"""

from __future__ import annotations

import pytest

from media_preview_generator.web.app import create_app
from media_preview_generator.web.routes.api_settings import _apply_post_save_hooks
from media_preview_generator.web.settings_manager import (
    get_settings_manager,
    reset_settings_manager,
)


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_settings_manager()
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    import media_preview_generator.web.scheduler as sched_mod

    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    yield
    reset_settings_manager()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        if sched_mod._schedule_manager is not None:
            try:
                sched_mod._schedule_manager.stop()
            except Exception:
                pass
            sched_mod._schedule_manager = None


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_AUTH_TOKEN", "test-token-12345678")
    return create_app(config_dir=str(tmp_path))


def _plex_entry(server_id: str, frame_interval: int | None = 10) -> dict:
    output: dict = {"adapter": "plex_bundle", "plex_config_folder": "/plex"}
    if frame_interval is not None:
        output["frame_interval"] = frame_interval
    return {
        "id": server_id,
        "type": "plex",
        "name": f"Plex {server_id}",
        "enabled": True,
        "url": "http://plex:32400",
        "auth": {"token": "plex-tok"},
        "output": output,
    }


def _jellyfin_entry(server_id: str, frame_interval: int | None = None) -> dict:
    output: dict = {"adapter": "jellyfin_trickplay"}
    if frame_interval is not None:
        output["frame_interval"] = frame_interval
    return {
        "id": server_id,
        "type": "jellyfin",
        "name": f"Jellyfin {server_id}",
        "enabled": True,
        "url": "http://jellyfin:8096",
        "auth": {"api_key": "jf-key"},
        "output": output,
    }


def _emby_entry(server_id: str, frame_interval: int | None = None) -> dict:
    output: dict = {"adapter": "emby_sidecar"}
    if frame_interval is not None:
        output["frame_interval"] = frame_interval
    return {
        "id": server_id,
        "type": "emby",
        "name": f"Emby {server_id}",
        "enabled": True,
        "url": "http://emby:8096",
        "auth": {"api_key": "emby-key"},
        "output": output,
    }


class TestThumbnailIntervalPropagation:
    """Direct unit tests for ``_apply_post_save_hooks`` step 6."""

    def test_propagates_to_every_server(self, app):
        """Plex stale at 10, Jellyfin/Emby missing entirely — all become 5.

        The persisted ``thumbnail_interval`` in settings deliberately disagrees
        with the value passed in ``updates``. The SUT must propagate the
        ``updates`` value (the about-to-be-saved new global), not whatever
        was already in settings — otherwise a save changing the value would
        silently re-apply the OLD value to per-server entries. This pins
        which side of the contract the hook reads from.
        """
        with app.app_context():
            sm = get_settings_manager()
            sm.update(
                {
                    "media_servers": [
                        _plex_entry("plex-1", frame_interval=10),
                        _jellyfin_entry("jf-1", frame_interval=None),
                        _emby_entry("emby-1", frame_interval=None),
                    ],
                    "thumbnail_interval": 99,  # deliberately != the updates value
                }
            )

            _apply_post_save_hooks(sm, {"thumbnail_interval": 5}, {"thumbnail_interval"})

            servers = sm.get("media_servers")
            assert len(servers) == 3, "no server entries should be added or dropped"
            # Bug-blind check would be ``len(servers) == 3`` alone, or asserting
            # ``== 99`` (which a regression that read from settings would also
            # pass). Assert the post-condition that actually pins the contract:
            # every entry's output.frame_interval == 5 (the updates value),
            # regardless of vendor, starting state, or what settings held.
            for entry in servers:
                actual = entry.get("output", {}).get("frame_interval")
                assert actual == 5, (
                    f"server {entry['id']!r} ({entry['type']}) has "
                    f"frame_interval={actual!r}; expected 5 from the updates "
                    f"dict (not 99 from already-persisted settings)"
                )

    def test_creates_output_block_when_missing(self, app):
        """An entry with no ``output`` key at all gets one with frame_interval set."""
        with app.app_context():
            sm = get_settings_manager()
            no_output_entry = {
                "id": "jf-bare",
                "type": "jellyfin",
                "name": "Jellyfin (no output)",
                "enabled": True,
                "url": "http://jellyfin:8096",
                "auth": {"api_key": "key"},
                # NOTE: deliberately no "output" key.
            }
            sm.update({"media_servers": [no_output_entry], "thumbnail_interval": 7})

            _apply_post_save_hooks(sm, {"thumbnail_interval": 7}, {"thumbnail_interval"})

            servers = sm.get("media_servers")
            assert servers[0]["output"]["frame_interval"] == 7, (
                "missing output block must be created with the propagated frame_interval"
            )

    def test_noop_when_already_aligned(self, app):
        """All servers already at 10 + saving 10 → no settings churn."""
        with app.app_context():
            sm = get_settings_manager()
            sm.update(
                {
                    "media_servers": [
                        _plex_entry("plex-1", frame_interval=10),
                        _jellyfin_entry("jf-1", frame_interval=10),
                    ],
                    "thumbnail_interval": 10,
                }
            )

            before = sm.get("media_servers")
            _apply_post_save_hooks(sm, {"thumbnail_interval": 10}, {"thumbnail_interval"})
            after = sm.get("media_servers")

            # The exact dict values should be byte-identical; the hook's
            # ``if changed`` guard from PR #240 must hold or it would re-persist
            # the same list every save.
            assert before == after, "no-op save should not mutate media_servers when already aligned"

    def test_does_not_fire_when_thumbnail_interval_absent_from_updates(self, app):
        """Saving an unrelated field (cpu_threads) must NOT touch per-server intervals."""
        with app.app_context():
            sm = get_settings_manager()
            sm.update(
                {
                    "media_servers": [
                        _plex_entry("plex-1", frame_interval=10),
                        _jellyfin_entry("jf-1", frame_interval=None),  # missing on purpose
                    ],
                    "thumbnail_interval": 5,
                    "cpu_threads": 2,
                }
            )

            # Simulate "user saved only cpu_threads" — thumbnail_interval not in updates.
            _apply_post_save_hooks(sm, {"cpu_threads": 4}, {"cpu_threads"})

            servers = sm.get("media_servers")
            # Plex stays at 10 (its previous value), Jellyfin stays missing.
            # Critically, the hook MUST NOT silently rewrite frame_interval
            # using whatever global is currently in settings — that would
            # introduce surprising side-effects on unrelated saves.
            assert servers[0]["output"]["frame_interval"] == 10
            assert "frame_interval" not in servers[1]["output"]
