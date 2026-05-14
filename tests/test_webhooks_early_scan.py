"""Tests for the Job-at-batch-open + early partial-scan webhook flow.

When a Sonarr/Radarr webhook arrives, the app now:

1. Creates a Job immediately (state = PENDING) so logs have somewhere to land.
2. Fires a path-only ``trigger_refresh`` on each owning server on a daemon
   thread, with the Job ID in scope so the log lines attribute to the Job.
3. Lets the existing 60s debounce timer carry on as today — when it fires,
   ``_execute_webhook_job`` looks up the pre-existing Job rather than
   creating a new one.

These tests pin the contract for that flow: Job timing, per-path scans on
batch merge, dedup-skip not creating a Job, and the helper's call shape.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.web.settings_manager import reset_settings_manager


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_settings_manager()
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    import media_preview_generator.web.webhooks as wh

    wh._webhook_history.clear()
    with wh._pending_lock:
        for t in wh._pending_timers.values():
            t.cancel()
        wh._pending_timers.clear()
        wh._pending_batches.clear()
        wh._recent_dispatches.clear()
    yield
    reset_settings_manager()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    wh._webhook_history.clear()
    with wh._pending_lock:
        for t in wh._pending_timers.values():
            t.cancel()
        wh._pending_timers.clear()
        wh._pending_batches.clear()
        wh._recent_dispatches.clear()


# ---------------------------------------------------------------------------
# Job-at-batch-open behavior
# ---------------------------------------------------------------------------


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_fresh_batch_creates_job_at_webhook_receipt_not_at_debounce_fire(mock_timer_cls, mock_job_mgr, mock_kick):
    """A fresh debounce batch must create the Job inside ``_schedule_webhook_job``.

    Pre-refactor, Job creation happened 60s later in ``_execute_webhook_job``,
    so the early scan-nudge had no Job to attribute its log lines to. The
    Job-at-batch-open contract: ``create_job`` is called synchronously from
    ``_schedule_webhook_job`` and again ``create_job`` is NOT called by
    ``_execute_webhook_job`` (which looks up the existing Job instead).
    """
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "job-abc"
    mock_job_mgr.return_value.create_job.return_value = mock_job
    mock_job_mgr.return_value.get_job.return_value = mock_job

    wh._schedule_webhook_job("radarr", "Test Movie", "/movies/test.mkv")

    # Job MUST be created at webhook receipt — that's the whole point.
    assert mock_job_mgr.return_value.create_job.call_count == 1
    call = mock_job_mgr.return_value.create_job.call_args
    assert call.kwargs["library_name"] == "Test Movie"
    assert call.kwargs["config"]["source"] == "radarr"
    assert call.kwargs["config"]["path_count"] == 1
    assert call.kwargs["config"]["webhook_basenames"] == ["test.mkv"]


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_batch_merge_updates_existing_job_does_not_create_a_second(mock_timer_cls, mock_job_mgr, mock_kick):
    """A second webhook joining the same debounce batch updates the existing Job."""
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "job-abc"
    mock_job_mgr.return_value.create_job.return_value = mock_job
    mock_job_mgr.return_value.get_job.return_value = mock_job

    wh._schedule_webhook_job("sonarr", "Show S01E01", "/tv/show/s01e01.mkv")
    wh._schedule_webhook_job("sonarr", "Show S01E02", "/tv/show/s01e02.mkv")

    # Exactly one Job for both webhooks.
    assert mock_job_mgr.return_value.create_job.call_count == 1
    # Library_name flips to "N files" once paths > 1.
    update_calls = mock_job_mgr.return_value.update_job_library_name.call_args_list
    assert any(c.args[1] == "2 files" for c in update_calls), update_calls
    # Config update fires for the batch-merge so the UI sees the new path count.
    cfg_updates = mock_job_mgr.return_value.update_job_config.call_args_list
    assert any(c.args[1]["path_count"] == 2 for c in cfg_updates), cfg_updates


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_kick_early_scan_called_per_webhook_with_job_id(mock_timer_cls, mock_job_mgr, mock_kick):
    """Each webhook fires its own scan-nudge for its own path, bound to the Job ID."""
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "job-xyz"
    mock_job_mgr.return_value.create_job.return_value = mock_job
    mock_job_mgr.return_value.get_job.return_value = mock_job

    wh._schedule_webhook_job("sonarr", "Show S01E01", "/tv/s01e01.mkv")
    wh._schedule_webhook_job("sonarr", "Show S01E02", "/tv/s01e02.mkv")

    # Each webhook gets its own scan-nudge with its own normalized path.
    # The job_id MUST be the batch's Job — every kick attributes to the
    # same Job because both webhooks share a debounce batch.
    assert mock_kick.call_count == 2
    paths = sorted(c.args[0] for c in mock_kick.call_args_list)
    assert paths == ["/tv/s01e01.mkv", "/tv/s01e02.mkv"]
    job_ids = {c.args[2] for c in mock_kick.call_args_list}
    assert job_ids == {"job-xyz"}


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_dedup_skip_does_not_create_job_or_fire_scan(mock_timer_cls, mock_job_mgr, mock_kick):
    """A duplicate webhook within dedup TTL must not produce a second Job or scan-nudge."""
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "job-1"
    mock_job_mgr.return_value.create_job.return_value = mock_job
    mock_job_mgr.return_value.get_job.return_value = mock_job

    accepted = wh._schedule_webhook_job("radarr", "Movie", "/m/x.mkv")
    deduped = wh._schedule_webhook_job("radarr", "Movie", "/m/x.mkv")

    assert accepted is True
    assert deduped is False
    assert mock_job_mgr.return_value.create_job.call_count == 1
    assert mock_kick.call_count == 1


# ---------------------------------------------------------------------------
# _execute_webhook_job uses the pre-created Job, not a new one
# ---------------------------------------------------------------------------


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
@patch("media_preview_generator.web.routes._start_job_async")
def test_execute_uses_existing_batch_job_does_not_call_create_job(
    mock_start_job, mock_timer_cls, mock_job_mgr, mock_settings, mock_kick
):
    """When the debounce timer fires, the Job is looked up — not re-created."""
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "job-existing"
    mock_job_mgr.return_value.create_job.return_value = mock_job
    mock_job_mgr.return_value.get_job.return_value = mock_job
    mock_settings.return_value.get.side_effect = lambda key, default=None: default

    wh._schedule_webhook_job("radarr", "Movie", "/m/x.mkv")
    create_count_before = mock_job_mgr.return_value.create_job.call_count

    wh._execute_webhook_job(wh._debounce_key("radarr"))

    # create_job count must NOT increase — _execute_webhook_job uses the
    # existing batch job. The lookup goes through get_job.
    assert mock_job_mgr.return_value.create_job.call_count == create_count_before
    mock_job_mgr.return_value.get_job.assert_called_with("job-existing")
    # _start_job_async still fires with the existing Job's id.
    assert mock_start_job.call_args[0][0] == "job-existing"


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
@patch("media_preview_generator.web.routes._start_job_async")
def test_execute_recreates_job_if_existing_was_deleted(
    mock_start_job, mock_timer_cls, mock_job_mgr, mock_settings, mock_kick
):
    """If the pre-created Job was deleted from the UI before the timer fired,
    ``_execute_webhook_job`` falls back to creating a fresh Job rather than crashing."""
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "job-1"
    mock_replacement = MagicMock()
    mock_replacement.id = "job-replacement"
    mock_job_mgr.return_value.create_job.side_effect = [mock_job, mock_replacement]
    # get_job returns None (job was deleted) the first time _execute_webhook_job
    # looks it up.
    mock_job_mgr.return_value.get_job.return_value = None
    mock_settings.return_value.get.side_effect = lambda key, default=None: default

    wh._schedule_webhook_job("radarr", "Movie", "/m/x.mkv")
    wh._execute_webhook_job(wh._debounce_key("radarr"))

    # First create_job at batch-open, second at recreate.
    assert mock_job_mgr.return_value.create_job.call_count == 2
    assert mock_start_job.call_args[0][0] == "job-replacement"


# ---------------------------------------------------------------------------
# _kick_early_scan helper behavior
# ---------------------------------------------------------------------------


def _drain_thread(name_prefix: str) -> None:
    """Wait for daemon threads matching ``name_prefix`` to finish."""
    import threading
    import time

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        live = [t for t in threading.enumerate() if t.name.startswith(name_prefix) and t.is_alive()]
        if not live:
            return
        for t in live:
            t.join(timeout=0.1)


@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.get_job_manager")
def test_kick_early_scan_calls_trigger_refresh_and_logs_to_job(mock_job_mgr, mock_settings):
    """The daemon thread fires ``trigger_refresh(item_id=None, remote_path=...)``
    on each owning server and logs success/failure to the Job."""
    from media_preview_generator.web import webhooks as wh

    fake_server = MagicMock()
    fake_server.name = "PlexLocal"
    fake_server.trigger_refresh.return_value = None

    fake_match = MagicMock()
    fake_match.server_id = "plex-1"

    fake_registry = MagicMock()
    fake_registry.configs.return_value = []
    fake_registry.get.return_value = fake_server

    mock_settings.return_value.get.side_effect = lambda key, default=None: default

    with (
        patch("media_preview_generator.servers.registry.ServerRegistry.from_settings", return_value=fake_registry),
        patch(
            "media_preview_generator.jobs.orchestrator._resolve_webhook_path_to_canonical",
            return_value=("/data/movies/test.mkv", [fake_match]),
        ),
    ):
        wh._kick_early_scan("/movies/test.mkv", server_id_filter=None, job_id="job-1")
        _drain_thread("early-scan-")

    fake_server.trigger_refresh.assert_called_once()
    call_kw = fake_server.trigger_refresh.call_args.kwargs
    assert call_kw["item_id"] is None
    assert call_kw["remote_path"] == "/data/movies/test.mkv"

    log_calls = mock_job_mgr.return_value.add_log.call_args_list
    assert any("INFO - Early scan-nudge sent to PlexLocal" in c.args[1] for c in log_calls), log_calls


@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.get_job_manager")
def test_kick_early_scan_logs_when_no_owners(mock_job_mgr, mock_settings):
    """If no configured server owns the path, the helper logs a skip and exits cleanly."""
    from media_preview_generator.web import webhooks as wh

    fake_registry = MagicMock()
    fake_registry.configs.return_value = []
    mock_settings.return_value.get.side_effect = lambda key, default=None: default

    with (
        patch("media_preview_generator.servers.registry.ServerRegistry.from_settings", return_value=fake_registry),
        patch(
            "media_preview_generator.jobs.orchestrator._resolve_webhook_path_to_canonical",
            return_value=("/orphan/x.mkv", []),
        ),
    ):
        wh._kick_early_scan("/orphan/x.mkv", server_id_filter=None, job_id="job-no-owners")
        _drain_thread("early-scan-")

    log_calls = mock_job_mgr.return_value.add_log.call_args_list
    assert any("INFO - Early scan-nudge: no configured server owns" in c.args[1] for c in log_calls), log_calls


@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.get_job_manager")
def test_kick_early_scan_respects_server_id_filter(mock_job_mgr, mock_settings):
    """server_id_filter narrows the scan to a single pinned server."""
    from media_preview_generator.web import webhooks as wh

    plex_server = MagicMock()
    plex_server.name = "Plex"
    emby_server = MagicMock()
    emby_server.name = "Emby"

    match_plex = MagicMock()
    match_plex.server_id = "plex-1"
    match_emby = MagicMock()
    match_emby.server_id = "emby-1"

    fake_registry = MagicMock()
    fake_registry.configs.return_value = []
    fake_registry.get.side_effect = lambda sid: {"plex-1": plex_server, "emby-1": emby_server}.get(sid)
    mock_settings.return_value.get.side_effect = lambda key, default=None: default

    with (
        patch("media_preview_generator.servers.registry.ServerRegistry.from_settings", return_value=fake_registry),
        patch(
            "media_preview_generator.jobs.orchestrator._resolve_webhook_path_to_canonical",
            return_value=("/data/x.mkv", [match_plex, match_emby]),
        ),
    ):
        wh._kick_early_scan("/data/x.mkv", server_id_filter="plex-1", job_id="job-pinned")
        _drain_thread("early-scan-")

    plex_server.trigger_refresh.assert_called_once()
    emby_server.trigger_refresh.assert_not_called()


@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.get_job_manager")
def test_kick_early_scan_swallows_per_server_failures(mock_job_mgr, mock_settings):
    """A failing trigger_refresh on one server must not abort the rest of the fan-out."""
    from media_preview_generator.web import webhooks as wh

    good_server = MagicMock()
    good_server.name = "Good"
    bad_server = MagicMock()
    bad_server.name = "Bad"
    bad_server.trigger_refresh.side_effect = RuntimeError("network down")

    match_good = MagicMock()
    match_good.server_id = "good"
    match_bad = MagicMock()
    match_bad.server_id = "bad"

    fake_registry = MagicMock()
    fake_registry.configs.return_value = []
    fake_registry.get.side_effect = lambda sid: {"good": good_server, "bad": bad_server}.get(sid)
    mock_settings.return_value.get.side_effect = lambda key, default=None: default

    with (
        patch("media_preview_generator.servers.registry.ServerRegistry.from_settings", return_value=fake_registry),
        patch(
            "media_preview_generator.jobs.orchestrator._resolve_webhook_path_to_canonical",
            return_value=("/data/x.mkv", [match_bad, match_good]),
        ),
    ):
        wh._kick_early_scan("/data/x.mkv", server_id_filter=None, job_id="job-fanout")
        _drain_thread("early-scan-")

    good_server.trigger_refresh.assert_called_once()
    bad_server.trigger_refresh.assert_called_once()
    log_calls = [c.args[1] for c in mock_job_mgr.return_value.add_log.call_args_list]
    assert any("WARNING - Early scan-nudge on Bad failed" in m for m in log_calls), log_calls
    assert any("INFO - Early scan-nudge sent to Good" in m for m in log_calls), log_calls


# ---------------------------------------------------------------------------
# Regression: webhook_paths MUST be persisted in job.config at batch-open and
# on every merge. Without this, a container restart during the 60s debounce
# window revives the job with no webhook_paths and the orchestrator silently
# falls through to a full library scan. See Job e7968486 (May 2026): one
# Sonarr webhook for a single TV episode triggered 8 full-library scans
# (128k items each) across 11 revivals because webhook_paths was None in
# the persisted config.
# ---------------------------------------------------------------------------


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_fresh_batch_persists_webhook_paths_in_job_config(mock_timer_cls, mock_job_mgr, mock_kick):
    """``create_job`` must receive ``webhook_paths`` in the config so that
    an auto-requeue after restart still has the path list to dispatch."""
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "job-abc"
    mock_job_mgr.return_value.create_job.return_value = mock_job
    mock_job_mgr.return_value.get_job.return_value = mock_job

    wh._schedule_webhook_job("sonarr", "Show S01E01", "/tv/show/s01e01.mkv")

    call = mock_job_mgr.return_value.create_job.call_args
    config = call.kwargs["config"]
    assert "webhook_paths" in config, (
        f"create_job config must include webhook_paths so revival "
        f"after restart can dispatch the path list — got keys {list(config.keys())}"
    )
    assert config["webhook_paths"] == ["/tv/show/s01e01.mkv"], config["webhook_paths"]


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_batch_merge_persists_full_webhook_paths_list(mock_timer_cls, mock_job_mgr, mock_kick):
    """On batch-merge, ``update_job_config`` must persist the full
    ``webhook_paths`` list (not just basenames + path_count) so revival
    finds every path the batch had accumulated."""
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "job-merge"
    mock_job_mgr.return_value.create_job.return_value = mock_job
    mock_job_mgr.return_value.get_job.return_value = mock_job

    wh._schedule_webhook_job("sonarr", "Show S01E01", "/tv/s01e01.mkv")
    wh._schedule_webhook_job("sonarr", "Show S01E02", "/tv/s01e02.mkv")
    wh._schedule_webhook_job("sonarr", "Show S01E03", "/tv/s01e03.mkv")

    # The merge-time update_job_config calls — must each carry the full
    # accumulated webhook_paths list, sorted for determinism.
    cfg_updates = mock_job_mgr.return_value.update_job_config.call_args_list
    # The last update reflects the final batch state.
    last_update_config = cfg_updates[-1].args[1]
    assert "webhook_paths" in last_update_config, (
        f"update_job_config must include webhook_paths so revival finds "
        f"all paths accumulated by the batch — got keys "
        f"{list(last_update_config.keys())}"
    )
    assert sorted(last_update_config["webhook_paths"]) == [
        "/tv/s01e01.mkv",
        "/tv/s01e02.mkv",
        "/tv/s01e03.mkv",
    ], last_update_config["webhook_paths"]
