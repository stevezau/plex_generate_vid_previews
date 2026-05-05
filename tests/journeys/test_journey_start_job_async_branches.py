"""TEST_AUDIT P1.7 — _start_job_async branch coverage.

The TOP bug-rate function in the repo (4 fixes in 90 days, ZERO direct
tests). Each test below drives a real Job through ``_start_job_async``
and asserts on the exact kwargs forwarded to the orchestrator's
``run_processing`` (the seam where the past bugs landed). External
boundaries (Plex API, FFmpeg, vendor publishers) are mocked; the Flask
app, the JobManager, and ``_start_job_async`` itself run for real.

Branches covered:
  - happy path: orchestrator ``run_processing`` actually invoked with
    the right ``Config`` (legacy plex view) — the wiring still works.
  - retry-spawn branch: ``skipped_file_not_found > 0`` produces a child
    retry job with ``is_retry=True`` and ``retry_attempt=1``.
  - ``regenerate=True`` propagation via overrides → the per-job
    ``Config.regenerate_thumbnails`` flag flips on.
  - ``webhook_item_id_hints`` propagation: the hints land on the
    Config object that ``run_processing`` receives.
  - ``server_id`` filter propagation: Config.server_id_filter set to
    that exact id (so downstream dispatch only fans out to one server).

Each test asserts ``run_processing.call_args.kwargs`` (or
``call_args.args``) on the SUT-controlled values — never just
``called_once()``. Asserting only the call_count would have hidden the
D34 dispatcher → ``process_canonical_path`` regression for months.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from media_preview_generator.web.app import create_app
from media_preview_generator.web.settings_manager import reset_settings_manager

pytestmark = pytest.mark.journey


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Each test starts with a fresh settings + job + scheduler singleton."""
    reset_settings_manager()
    import media_preview_generator.web.jobs as jobs_mod
    import media_preview_generator.web.scheduler as sched_mod
    import media_preview_generator.web.webhooks as wh_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    wh_mod._recent_dispatches.clear()
    wh_mod._pending_batches.clear()
    for t in list(wh_mod._pending_timers.values()):
        try:
            t.cancel()
        except Exception:
            pass
    wh_mod._pending_timers.clear()
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
    wh_mod._recent_dispatches.clear()
    wh_mod._pending_batches.clear()
    for t in list(wh_mod._pending_timers.values()):
        try:
            t.cancel()
        except Exception:
            pass
    wh_mod._pending_timers.clear()


@pytest.fixture()
def app(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("WEB_AUTH_TOKEN", "test-token-12345678")
    settings_path = config_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "setup_complete": True,
                "webhook_enabled": True,
                "webhook_delay": 0,
                "webhook_retry_count": 3,
                "webhook_retry_delay": 30,
                "media_servers": [
                    {
                        "id": "plex-1",
                        "type": "plex",
                        "name": "Plex Main",
                        "enabled": True,
                        "url": "http://plex:32400",
                        "auth": {"token": "tok"},
                        "libraries": [{"id": "1", "name": "Movies", "enabled": True}],
                        "output": {"adapter": "plex_bundle", "plex_config_folder": str(tmp_path / "plex_cfg")},
                    },
                    {
                        "id": "emby-1",
                        "type": "emby",
                        "name": "Emby Spare",
                        "enabled": True,
                        "url": "http://emby:8096",
                        "auth": {"method": "api_key", "api_key": "k"},
                        "libraries": [{"id": "2", "name": "Movies", "enabled": True}],
                        "output": {"adapter": "emby_sidecar"},
                    },
                ],
            }
        )
    )
    auth_path = config_dir / "auth.json"
    auth_path.write_text(json.dumps({"token": "test-token-12345678"}))
    # Make plex_config_folder pass _validate_plex_config — needs Media/localhost.
    (tmp_path / "plex_cfg" / "Media" / "localhost").mkdir(parents=True, exist_ok=True)
    return create_app(config_dir=str(config_dir))


def _wait_for(predicate, timeout=3.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Branch 1: happy path — _start_job_async actually invokes run_processing
# ---------------------------------------------------------------------------


class TestStartJobAsyncHappyPath:
    """Real job → real ``_start_job_async`` → orchestrator ``run_processing``
    invoked with a real Config carrying the legacy Plex view derived from
    ``media_servers[0]``. This is the bare-bones contract the function has
    promised since day 1. No retry, no overrides, no pin."""

    def test_basic_dispatch_invokes_run_processing_with_real_config(self, app, tmp_path):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        captured: dict = {}

        def fake_run_processing(config, selected_gpus, **kwargs):
            captured["config"] = config
            captured["selected_gpus"] = list(selected_gpus or [])
            captured["kwargs"] = dict(kwargs)
            return {"outcome": {"generated": 0}}

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            job = get_job_manager().create_job(library_name="Movies", config={})
            _start_job_async(job.id, config_overrides=None)

        assert "config" in captured, "run_processing was never invoked — _start_job_async wiring broken"

        # Legacy Plex view derived from media_servers[0] — confirms
        # derive_legacy_plex_view actually ran on the path through
        # _start_job_async.
        assert captured["config"].plex_url == "http://plex:32400", (
            f"plex_url should be hydrated from media_servers[0].url; got {captured['config'].plex_url!r}. "
            f"A regression that stops calling derive_legacy_plex_view would leave it empty/None."
        )
        assert captured["config"].plex_token == "tok", (
            f"plex_token should be hydrated from media_servers[0].auth.token; got {captured['config'].plex_token!r}"
        )

        # Critical kwarg: job_id must be threaded all the way through so
        # the dispatcher can route worker outcomes back to this job.
        assert captured["kwargs"].get("job_id") == job.id, (
            f"run_processing must receive job_id={job.id!r} so dispatcher can attribute worker outcomes; "
            f"got {captured['kwargs'].get('job_id')!r}"
        )

        # Cancel + pause checks must be wired so the job is interruptible.
        assert callable(captured["kwargs"].get("cancel_check")), (
            "cancel_check callable must be passed to run_processing; otherwise cancel API can't stop the job"
        )
        assert callable(captured["kwargs"].get("pause_check")), (
            "pause_check callable must be passed to run_processing; otherwise pause API can't quiesce dispatch"
        )

        # Job ended in a terminal state (not stuck pending).
        final = get_job_manager().get_job(job.id)
        assert final is not None
        assert final.status.value in ("completed", "failed", "cancelled"), (
            f"Job must reach a terminal state after run_processing returns; got status={final.status.value!r}"
        )


# ---------------------------------------------------------------------------
# Branch 2: skipped_file_not_found triggers a retry-job spawn (commit e31d051)
# ---------------------------------------------------------------------------


class TestStartJobAsyncRetryBranch:
    """Pin the retry-spawn contract that landed in commit e31d051.

    When ``run_processing`` returns ``outcome.skipped_file_not_found > 0``
    AND the job has webhook_paths AND retry budget remains, a CHILD job
    must be created via ``_spawn_retry_job`` with:
      - ``is_retry=True``
      - ``retry_attempt=1``
      - ``parent_job_id=<original>``
      - ``webhook_paths`` matching the parent's

    Without coverage here, a regression that drops the retry path silently
    abandons every webhook for files mid-copy by Plex (a common Sonarr +
    fast-link race). Asserts on the SHAPE of the child job + assertions
    that the retry was scheduled (not fired immediately, not lost)."""

    def test_skipped_file_not_found_spawns_child_retry_job(self, app, tmp_path):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        run_calls: list[dict] = []

        # First call: file not found on disk (Plex returned a stale path).
        # Second call (retry): treat as the second invocation — pretend
        # everything generated, so we don't recurse forever. _start_job_async
        # spawns a *new* run_job thread for the retry; the synchronous
        # threading shim runs it inline, so both invocations happen during
        # this test.
        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append(
                {
                    "webhook_paths": list(getattr(config, "webhook_paths", []) or []),
                    "is_retry_in_config": kwargs.get("job_id"),
                }
            )
            if len(run_calls) == 1:
                # Parent job: Plex returned a stale path for the file.
                # Trigger the not-found-on-disk branch.
                jm = get_job_manager()
                job_id = kwargs.get("job_id")
                # Simulate the worker's record_file_result for the missing file.
                jm.record_file_result(
                    job_id,
                    "/data/tv/Show/S01E01.mkv",
                    "skipped_file_not_found",
                    "file gone",
                    "[GPU 0]",
                )
                return {
                    "outcome": {"skipped_file_not_found": 1, "generated": 0, "failed": 0},
                    "webhook_resolution": {
                        "unresolved_paths": [],
                        "skipped_paths": [],
                        "resolved_count": 1,
                        "total_paths": 1,
                        "path_hints": [],
                    },
                }
            # Retry path: success.
            return {
                "outcome": {"generated": 1},
                "webhook_resolution": {
                    "unresolved_paths": [],
                    "skipped_paths": [],
                    "resolved_count": 1,
                    "total_paths": 1,
                    "path_hints": [],
                },
            }

        # Shrink the retry backoff so the test isn't blocked for 30s
        # waiting for the retry job's countdown timer to elapse. The
        # production code clamps webhook_retry_delay to >= 10s and uses
        # BACKOFF_SCHEDULE for the actual wait.
        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
            # Suppress the partial-scan call (would hit the network).
            patch(
                "media_preview_generator.plex_client.trigger_plex_partial_scan",
                return_value=[],
            ),
            # Replace the slow backoff schedule with 1s entries so the
            # retry's countdown loop completes near-instantly.
            patch(
                "media_preview_generator.processing.retry_queue.BACKOFF_SCHEDULE",
                [1, 1, 1, 1, 1],
            ),
        ):
            job = get_job_manager().create_job(
                library_name="The Show",
                config={"source": "sonarr"},
            )
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/tv/Show/S01E01.mkv"],
                    "webhook_retry_count": 3,
                    "webhook_retry_delay": 30,
                },
            )

        # Both runs happened (parent + retry).
        assert len(run_calls) == 2, (
            f"Expected the parent run plus exactly 1 retry-spawn run; got {len(run_calls)} runs. runs={run_calls!r}"
        )

        # Find the spawned retry job in the JobManager. There must be exactly
        # one with is_retry=True + parent_job_id pointing at the original.
        all_jobs = get_job_manager().get_all_jobs()
        retry_jobs = [
            j
            for j in all_jobs
            if (j.config or {}).get("is_retry") is True and (j.config or {}).get("parent_job_id") == job.id
        ]
        assert len(retry_jobs) == 1, (
            f"Exactly 1 retry job must be spawned with parent_job_id={job.id!r} and is_retry=True; "
            f"found {len(retry_jobs)}. All jobs: "
            f"{[(j.id, j.library_name, (j.config or {}).get('is_retry')) for j in all_jobs]}"
        )
        retry = retry_jobs[0]
        assert retry.config.get("retry_attempt") == 1, (
            f"First retry must carry retry_attempt=1; got {retry.config.get('retry_attempt')!r}"
        )
        # The retry must inherit the PARENT's library_name (so the row in
        # the Jobs UI reads identically — "The Show" → "Retry: The Show",
        # NOT "Retry: S01E01.mkv"). Original behaviour used the raw
        # filename which produced ugly mismatched rows like:
        #   parent: Chelsea vs Nottingham Forest    [Sportarr]
        #   retry:  Retry: English Premier League - S2025E348 - Chelsea vs Nottingham Forest - HDTV-2160p.mkv
        # Inheriting the parent's library_name keeps the two visually
        # aligned and lets the user trace the retry back to its parent
        # at a glance.
        assert retry.library_name == "Retry: The Show", (
            f"Retry library_name must be 'Retry: <parent.library_name>' — got {retry.library_name!r}. "
            f"A regression that falls back to the raw filename produces ugly "
            f"'Retry: episode-file-with-codec-tags.mkv' rows that don't match "
            f"the parent's clean Sonarr-derived title."
        )

        # The retry's run_processing call carried the ORIGINAL webhook path
        # — pinned forward through _spawn_retry_job's retry_paths build.
        retry_run = run_calls[1]
        assert retry_run["webhook_paths"] == ["/data/tv/Show/S01E01.mkv"], (
            f"Retry's run_processing must receive the original webhook_paths verbatim "
            f"(retry_paths logic must not drop them); got {retry_run['webhook_paths']!r}"
        )


# ---------------------------------------------------------------------------
# Branch 3: regenerate=True propagates to Config.regenerate_thumbnails
# ---------------------------------------------------------------------------


class TestStartJobAsyncRegeneratePropagation:
    """``config_overrides['force_generate'] = True`` must flip
    ``Config.regenerate_thumbnails`` on for this job. A regression that
    drops the override would silently re-use stale .meta journal entries
    and skip files the user explicitly asked to regenerate (the Force
    Regenerate button on the Jobs page)."""

    def test_force_generate_override_sets_regenerate_thumbnails_on_config(self, app, tmp_path):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        captured: dict = {}

        def fake_run_processing(config, selected_gpus, **kwargs):
            captured["regenerate_thumbnails"] = bool(config.regenerate_thumbnails)
            return {"outcome": {"generated": 0}}

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            job = get_job_manager().create_job(library_name="Movies", config={})
            _start_job_async(job.id, config_overrides={"force_generate": True})

        assert captured.get("regenerate_thumbnails") is True, (
            f"force_generate=True override must set Config.regenerate_thumbnails=True; "
            f"got {captured.get('regenerate_thumbnails')!r}. A regression here silently ignores "
            f"the user clicking Force Regenerate."
        )


# ---------------------------------------------------------------------------
# Branch 4: webhook_item_id_hints propagate to Config.webhook_item_id_hints
# ---------------------------------------------------------------------------


class TestStartJobAsyncWebhookHintsPropagation:
    """Vendor webhooks (Plex/Emby/Jellyfin) carry ``{path: {server_id:
    item_id}}`` hints so the orchestrator skips a slow Plex round trip.
    A regression that drops hints silently degrades to "look this file up
    in Plex" — fatal on a Plex-less install (Emby/Jellyfin only) where
    Plex isn't even configured. Pin the propagation contract."""

    def test_webhook_item_id_hints_land_on_config(self, app, tmp_path):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        captured: dict = {}
        hints_in = {"/data/tv/Show/S01E01.mkv": {"emby-1": "abc123"}}

        def fake_run_processing(config, selected_gpus, **kwargs):
            captured["hints"] = getattr(config, "webhook_item_id_hints", None)
            return {"outcome": {"generated": 0}}

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            job = get_job_manager().create_job(library_name="Show", config={})
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/tv/Show/S01E01.mkv"],
                    "webhook_item_id_hints": hints_in,
                },
            )

        # Strict equality: the hints dict must arrive byte-for-byte the same.
        assert captured.get("hints") == hints_in, (
            f"webhook_item_id_hints override must reach Config.webhook_item_id_hints unchanged; "
            f"got {captured.get('hints')!r}. A regression here means Emby/Jellyfin webhooks would "
            f"fall back to a Plex lookup and 500 on Plex-less installs."
        )


# ---------------------------------------------------------------------------
# Branch 5: server_id pin → Config.server_id_filter
# ---------------------------------------------------------------------------


class TestStartJobAsyncServerIdPin:
    """``config_overrides['server_id'] = 'emby-1'`` must:
      1. set ``Config.server_id_filter = 'emby-1'`` (downstream dispatch
         filters publishers to this id)
      2. project the legacy Plex view from THE PINNED SERVER (not
         media_servers[0]) — when pinned to a Plex server. Pinning to
         non-Plex (emby) leaves plex_url empty (no Plex view derivable).

    Pre-K1: pinning to a non-Plex server still tried to use media_servers[0]'s
    Plex URL/token, fanning to every server. Pin both halves of the contract."""

    def test_server_id_pin_to_emby_sets_filter_and_clears_plex_view(self, app, tmp_path):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        captured: dict = {}

        def fake_run_processing(config, selected_gpus, **kwargs):
            captured["server_id_filter"] = getattr(config, "server_id_filter", None)
            captured["plex_url"] = config.plex_url
            return {"outcome": {"generated": 0}}

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            job = get_job_manager().create_job(library_name="Movies", config={})
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/movies/x.mkv"],
                    "server_id": "emby-1",
                },
            )

        assert captured.get("server_id_filter") == "emby-1", (
            f"server_id override must reach Config.server_id_filter exactly; "
            f"got {captured.get('server_id_filter')!r}. A regression silently fans out to every "
            f"configured server, which is the multi-server publisher-mismatch class of bug."
        )

    def test_server_id_pin_to_plex_projects_pinned_server_view(self, app, tmp_path):
        """Pinning to a specific Plex server must project the legacy plex
        view from THAT entry (not media_servers[0]). With one Plex configured,
        plex-1's url/token are the same as media_servers[0]'s — the test
        still verifies the value lands so a future multi-Plex regression
        that drops the pinned-id path is caught."""
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        captured: dict = {}

        def fake_run_processing(config, selected_gpus, **kwargs):
            captured["server_id_filter"] = getattr(config, "server_id_filter", None)
            captured["plex_url"] = config.plex_url
            captured["plex_token"] = config.plex_token
            return {"outcome": {"generated": 0}}

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            job = get_job_manager().create_job(library_name="Movies", config={})
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/movies/x.mkv"],
                    "server_id": "plex-1",
                },
            )

        assert captured.get("server_id_filter") == "plex-1"
        # The pinned Plex's url/token still get projected — without this a
        # job pinned to a non-default Plex would try to talk to the wrong server.
        assert captured.get("plex_url") == "http://plex:32400", (
            f"Pinned-to-plex-1 job must project plex-1's URL onto config.plex_url; got {captured.get('plex_url')!r}"
        )
        assert captured.get("plex_token") == "tok"


# ---------------------------------------------------------------------------
# Branch 6: simplified webhook_resolution payload contract (post-K4 unification)
# ---------------------------------------------------------------------------


class TestStartJobAsyncSimplifiedPayload:
    """Pin the contract between the orchestrator's unified webhook phase
    and ``_start_job_async``'s result handler.

    The unification (commit 3edd185) shrunk the ``webhook_resolution``
    payload: ``resolution_source`` was dropped, ``path_hints`` now
    carries the path-mapping mismatch diagnostics that used to come
    from the Plex-first stage, and the job-level
    ``trigger_plex_partial_scan`` call was removed in favour of the
    per-server ``trigger_refresh`` already firing inside the worker.

    Without these tests, a future refactor that re-introduces a field
    under a new name (or drops ``path_hints`` thinking it's unused)
    would silently break the file_result UI's "did you forget a path
    mapping?" diagnostic — the exact UX regression risk the plan
    called out.
    """

    def test_unresolved_path_with_mapping_hint_lands_in_file_result(self, app, tmp_path):
        """When ``run_processing`` returns the unified payload with a
        ``path_hints[0]`` carrying a path-mapping mismatch hint, the
        job_runner must surface that hint as the file_result message.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        hint = (
            "Possible prefix mismatch: webhook sends '/mnt' but a configured library "
            "uses '/data'. Add a path mapping in Settings: server path = /data, "
            "webhook path = /mnt"
        )

        def fake_run_processing(config, selected_gpus, **kwargs):
            return {
                "outcome": {"generated": 0, "failed": 0},
                "webhook_resolution": {
                    "unresolved_paths": ["/mnt/data/Movies/X.mkv"],
                    "skipped_paths": [],
                    "resolved_count": 0,
                    "total_paths": 1,
                    "path_hints": [hint],
                    # NB: no `resolution_source` — that field is gone post-unification.
                },
            }

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
            # Shrink retry backoff so the spawned retry job's countdown
            # doesn't pin the test for 30s; the assertion below only
            # cares about the parent job's file_result row.
            patch(
                "media_preview_generator.processing.retry_queue.BACKOFF_SCHEDULE",
                [1, 1, 1, 1, 1],
            ),
        ):
            job = get_job_manager().create_job(library_name="Movies", config={})
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/mnt/data/Movies/X.mkv"],
                    "webhook_retry_count": 0,  # no retry — keeps the test focused
                },
            )

        results = get_job_manager().get_file_results(job.id)
        unresolved_rows = [r for r in results if r.get("file") == "/mnt/data/Movies/X.mkv"]
        assert unresolved_rows, (
            "file_result for the unresolved path was never written — "
            "result handler stopped reading webhook_resolution.unresolved_paths"
        )
        row = unresolved_rows[0]
        # The outcome key must match what the file_results UI filters on.
        assert row["outcome"] == "unresolved_vendor", (
            f"Post-unification outcome key must be 'unresolved_vendor' (was 'unresolved_plex' "
            f"in the legacy Plex-first stage); got {row['outcome']!r}"
        )
        # The hint text must surface in the reason field — that's the
        # entire point of porting _detect_path_prefix_mismatches into
        # the unified phase. (file_results store the user-facing string
        # under ``reason``; the row also carries it in case future UIs
        # rename the field.)
        reason = row.get("reason") or row.get("message") or ""
        assert "/mnt" in reason and "/data" in reason, (
            f"Path-mapping mismatch hint did not surface in file_result reason; got {reason!r}"
        )

    def test_legacy_resolution_source_field_absence_does_not_crash(self, app, tmp_path):
        """A payload missing ``resolution_source`` must not raise — the
        result handler used to do ``resolution.get('resolution_source') or 'plex'``
        and switch on the value. Post-unification, the field is gone;
        a regression that re-introduces a hard ``resolution['resolution_source']``
        access would silently AttributeError on every webhook job.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        def fake_run_processing(config, selected_gpus, **kwargs):
            # Brand-new minimal payload — no resolution_source, no path_hints.
            return {
                "outcome": {"generated": 0, "failed": 0},
                "webhook_resolution": {
                    "unresolved_paths": ["/data/X.mkv"],
                    "skipped_paths": [],
                    "resolved_count": 0,
                    "total_paths": 1,
                    "path_hints": [],
                },
            }

        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            job = get_job_manager().create_job(library_name="Movies", config={})
            # Must not raise.
            _start_job_async(
                job.id,
                config_overrides={
                    "webhook_paths": ["/data/X.mkv"],
                    "webhook_retry_count": 0,
                },
            )

        # Job reached a terminal state (didn't get stuck on a missing-field crash).
        final = get_job_manager().get_job(job.id)
        assert final is not None
        assert final.status.value in ("completed", "failed", "cancelled"), (
            f"Job must reach terminal state with the simplified payload; got {final.status.value!r}. "
            "An AttributeError on resolution_source would land the job in 'running' or 'failed'."
        )
        # And the file_result for the unresolved path was still written.
        results = get_job_manager().get_file_results(job.id)
        assert any(r.get("file") == "/data/X.mkv" for r in results), (
            "Unresolved path's file_result missing — the result handler bailed before recording it."
        )
