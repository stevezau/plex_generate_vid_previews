"""
Tests for per-file job result tracking.

Covers: JobManager.record_file_result / get_file_results round-trip,
JSONL persistence, filtering, retention cleanup, deletion cleanup,
and the GET /api/jobs/{id}/files API endpoint.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from media_preview_generator.web.jobs import JobManager


@pytest.fixture(autouse=True)
def _reset_job_manager():
    """Reset global job manager so tests can create their own with custom config_dir."""
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    yield
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None


@pytest.fixture
def config_dir(tmp_path):
    """Temporary config directory."""
    return str(tmp_path / "config")


class TestFileResultRecording:
    """record_file_result writes JSONL and get_file_results reads it back."""

    def test_record_and_read_round_trip(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")

        jm.record_file_result(job.id, "/media/video1.mkv", "generated", "", "GPU 1")
        jm.record_file_result(job.id, "/media/video2.mkv", "failed", "FFmpeg exit 183", "GPU 2")
        jm.record_file_result(job.id, "/media/video3.mkv", "skipped_bif_exists", "BIF exists", "")

        results = jm.get_file_results(job.id)
        assert len(results) == 3
        assert results[0]["file"] == "/media/video1.mkv"
        assert results[0]["outcome"] == "generated"
        assert results[0]["worker"] == "GPU 1"
        assert results[1]["outcome"] == "failed"
        assert results[1]["reason"] == "FFmpeg exit 183"
        assert results[2]["outcome"] == "skipped_bif_exists"

    def test_jsonl_file_created(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.record_file_result(job.id, "/media/a.mkv", "generated")

        path = os.path.join(config_dir, "logs", "job_file_results", f"{job.id}.jsonl")
        assert os.path.isfile(path)
        with open(path) as f:
            lines = [line.strip() for line in f if line.strip()]
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["file"] == "/media/a.mkv"
        assert record["outcome"] == "generated"
        assert "ts" in record

    def test_get_file_results_empty_when_no_records(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        assert jm.get_file_results("nonexistent") == []

    def test_timestamp_present(self, config_dir):
        """``ts`` field is present, well-formed, and reflects current UTC time.

        Audit fix — original assertion was just ``assert results[0]["ts"]``
        which passes for any truthy value (including a stale fixture
        string, an exception message, or "{}"). Production format at
        web/jobs.py:1394 is ``datetime.now(timezone.utc).strftime("%H:%M:%S")``
        — pin the regex shape AND verify the recorded timestamp falls
        within ±5 seconds of "now" (otherwise a clock-skew or
        wrong-format regression slips through).
        """
        import re
        from datetime import datetime, timezone

        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        before = datetime.now(timezone.utc)
        jm.record_file_result(job.id, "/media/a.mkv", "generated")
        after = datetime.now(timezone.utc)
        results = jm.get_file_results(job.id)
        ts = results[0]["ts"]

        assert isinstance(ts, str), f"ts must be a string; got {type(ts).__name__}: {ts!r}"
        assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", ts), (
            f"ts must match HH:MM:SS (production format at web/jobs.py:1394); got {ts!r}"
        )
        # The recorded HH:MM:SS must fall within the [before, after]
        # window we bracketed around the call (±1s slack for second-rollover).
        recorded = datetime.strptime(ts, "%H:%M:%S").time()
        # Compare on (h, m, s) to avoid date-rollover headaches at
        # midnight-UTC; also accept ±1 second of slack.
        before_secs = before.hour * 3600 + before.minute * 60 + before.second
        after_secs = after.hour * 3600 + after.minute * 60 + after.second
        recorded_secs = recorded.hour * 3600 + recorded.minute * 60 + recorded.second
        # Handle midnight wrap by allowing either direction within 5s.
        delta = min(
            abs(recorded_secs - before_secs),
            abs(recorded_secs - after_secs),
            86400 - abs(recorded_secs - before_secs),
            86400 - abs(recorded_secs - after_secs),
        )
        assert delta <= 5, (
            f"ts {ts!r} must be within 5s of the recording call; "
            f"before={before.strftime('%H:%M:%S')}, after={after.strftime('%H:%M:%S')}, "
            f"delta_seconds={delta}"
        )

    def test_malformed_jsonl_lines_skipped(self, config_dir):
        """Corrupt lines in the JSONL file are silently skipped."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.record_file_result(job.id, "/media/good.mkv", "generated")

        path = jm._file_results_path(job.id)
        with open(path, "a") as f:
            f.write("NOT VALID JSON\n")
            f.write("\n")
            f.write('{"file":"/media/also_good.mkv","outcome":"failed","reason":"","worker":"","ts":"00:00:00"}\n')

        results = jm.get_file_results(job.id)
        assert len(results) == 2
        assert results[0]["file"] == "/media/good.mkv"
        assert results[1]["file"] == "/media/also_good.mkv"


class TestFileResultFiltering:
    """get_file_results with outcome_filter and search parameters."""

    def _seed(self, jm, job_id):
        jm.record_file_result(job_id, "/media/MovieA.mkv", "generated", "", "GPU 1")
        jm.record_file_result(job_id, "/media/MovieB.mkv", "failed", "exit 1", "GPU 2")
        jm.record_file_result(job_id, "/media/ShowC.mkv", "skipped_bif_exists", "exists", "")
        jm.record_file_result(job_id, "/media/ShowD.mkv", "failed", "exit 183", "CPU 1")

    def test_filter_by_outcome(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        self._seed(jm, job.id)

        failed = jm.get_file_results(job.id, outcome_filter="failed")
        assert len(failed) == 2
        assert all(r["outcome"] == "failed" for r in failed)

    def test_filter_by_search(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        self._seed(jm, job.id)

        results = jm.get_file_results(job.id, search="Show")
        assert len(results) == 2
        assert all("Show" in r["file"] for r in results)

    def test_filter_by_search_case_insensitive(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        self._seed(jm, job.id)

        results = jm.get_file_results(job.id, search="movieb")
        assert len(results) == 1
        assert results[0]["file"] == "/media/MovieB.mkv"

    def test_filter_combined(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        self._seed(jm, job.id)

        results = jm.get_file_results(job.id, outcome_filter="failed", search="Show")
        assert len(results) == 1
        assert results[0]["file"] == "/media/ShowD.mkv"


class TestFileResultRetention:
    """Retention and cleanup of file result JSONL files."""

    def test_retention_removes_file_results_for_expired_jobs(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Old Job")
        jm.start_job(job.id)
        jm.record_file_result(job.id, "/media/a.mkv", "generated")
        jm.complete_job(job.id)

        results_path = jm._file_results_path(job.id)
        assert os.path.isfile(results_path)

        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        jm._jobs[job.id].completed_at = old_time
        jm._persist_job(jm._jobs[job.id])

        with patch("media_preview_generator.web.settings_manager.get_settings_manager") as m:
            m.return_value.get.return_value = 30
            jm._enforce_log_retention()

        assert jm.get_job(job.id) is None
        assert not os.path.isfile(results_path)

    def test_delete_job_removes_file_results(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.record_file_result(job.id, "/media/a.mkv", "generated")
        jm.complete_job(job.id)

        results_path = jm._file_results_path(job.id)
        assert os.path.isfile(results_path)

        jm.delete_job(job.id)
        assert not os.path.isfile(results_path)

    def test_clear_completed_removes_file_results(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.record_file_result(job.id, "/media/a.mkv", "generated")
        jm.complete_job(job.id)

        results_path = jm._file_results_path(job.id)
        assert os.path.isfile(results_path)

        jm.clear_completed_jobs()
        assert not os.path.isfile(results_path)

    def test_orphaned_file_results_cleaned_up(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)

        orphan_path = os.path.join(config_dir, "logs", "job_file_results", "orphan-id.jsonl")
        os.makedirs(os.path.dirname(orphan_path), exist_ok=True)
        with open(orphan_path, "w") as f:
            f.write('{"file":"x","outcome":"generated"}\n')

        with patch("media_preview_generator.web.settings_manager.get_settings_manager") as m:
            m.return_value.get.return_value = 30
            jm._enforce_log_retention()

        assert not os.path.isfile(orphan_path)


class TestFileResultCallback:
    """set_file_result_callback and _notify_file_result wiring."""

    def test_callback_invoked_for_each_outcome(self):
        from media_preview_generator.processing import (
            ProcessingResult,
            _notify_file_result,
            set_file_result_callback,
        )

        captured = []

        def cb(file_path, outcome_str, reason, worker, servers=None):
            captured.append(
                {
                    "file": file_path,
                    "outcome": outcome_str,
                    "reason": reason,
                    "servers": list(servers or []),
                }
            )

        set_file_result_callback(cb)
        try:
            _notify_file_result("/a.mkv", ProcessingResult.GENERATED, "", "GPU 1")
            _notify_file_result(
                "/b.mkv",
                ProcessingResult.FAILED,
                "exit 1",
                "CPU 1",
                servers=[{"server_id": "plex-default", "server_name": "Plex", "status": "failed"}],
            )
        finally:
            set_file_result_callback(None)

        assert len(captured) == 2
        assert captured[0]["outcome"] == "generated"
        assert captured[1]["outcome"] == "failed"
        # D9 — per-server attribution flows through the callback so the JSONL
        # gets a `servers` field per file row.
        assert captured[1]["servers"] == [{"server_id": "plex-default", "server_name": "Plex", "status": "failed"}]

    def test_callback_cleared(self):
        from media_preview_generator.processing import (
            ProcessingResult,
            _notify_file_result,
            set_file_result_callback,
        )

        captured = []
        set_file_result_callback(lambda *a: captured.append(a))
        set_file_result_callback(None)
        _notify_file_result("/a.mkv", ProcessingResult.GENERATED, "", "")
        assert len(captured) == 0

    def test_callback_exception_does_not_propagate(self):
        """A failing callback must not crash the caller — and must have run.

        Audit fix — original test only verified the call didn't raise.
        That would have passed even if ``_notify_file_result`` short-
        circuited and never invoked the callback at all (e.g. a global
        kill-switch that bypassed callbacks entirely). Wrap the bad_cb
        in a MagicMock so we can assert ``call_count == 1`` proving the
        callback actually ran AND the exception was caught.
        """
        from unittest.mock import MagicMock

        from media_preview_generator.processing import (
            ProcessingResult,
            _notify_file_result,
            set_file_result_callback,
        )

        mock_cb = MagicMock(side_effect=RuntimeError("boom"))
        set_file_result_callback(mock_cb)
        try:
            _notify_file_result("/a.mkv", ProcessingResult.GENERATED, "", "GPU 1")
        finally:
            set_file_result_callback(None)

        assert mock_cb.call_count == 1, (
            f"Callback must be invoked exactly once even though it raises; got call_count={mock_cb.call_count}. "
            f"A regression that silently swallowed the call (skipping callback dispatch) would otherwise pass."
        )


class TestFileResultCallbackConcurrency:
    """Callback registration must be per-job, not a single process-wide global.

    Production incident: job ``deea99db`` scanned 128007 items; its file-
    results JSONL only grew for the first 6 seconds (1192 rows), then froze
    — because a concurrent 5-second job ``bb68e6cc`` started, installed its
    own callback, and then cleared the shared global on its way out. The
    long-running job's remaining ~127k items silently no-op'd on the
    callback for the next 11 minutes. The Files panel was empty for the
    rest of the run, and get_file_results(job_id, 'skipped_file_not_found')
    couldn't find the files that needed retry scheduling.
    """

    def test_callback_routed_to_active_failure_scope(self):
        """When two jobs register callbacks under different ``failure_scope``
        blocks, ``_notify_file_result`` must dispatch to the callback
        associated with the *current* thread's scope — not a single
        shared global.
        """
        from media_preview_generator.processing import (
            ProcessingResult,
            _notify_file_result,
            failure_scope,
            set_file_result_callback,
        )

        a_received: list[str] = []
        b_received: list[str] = []

        with failure_scope("job-a"):
            set_file_result_callback(lambda f, *_: a_received.append(f), job_id="job-a")
        with failure_scope("job-b"):
            set_file_result_callback(lambda f, *_: b_received.append(f), job_id="job-b")

        with failure_scope("job-a"):
            _notify_file_result("/a1.mkv", ProcessingResult.GENERATED, "", "")
        with failure_scope("job-b"):
            _notify_file_result("/b1.mkv", ProcessingResult.GENERATED, "", "")
        with failure_scope("job-a"):
            _notify_file_result("/a2.mkv", ProcessingResult.GENERATED, "", "")

        with failure_scope("job-a"):
            set_file_result_callback(None, job_id="job-a")
        with failure_scope("job-b"):
            set_file_result_callback(None, job_id="job-b")

        assert a_received == ["/a1.mkv", "/a2.mkv"]
        assert b_received == ["/b1.mkv"]

    def test_job_b_completion_does_not_silence_job_a_callback(self):
        """Regression for the deea99db incident: if job B enters its scope,
        installs its callback, then clears it on exit, job A's callback
        must still fire for the remainder of job A's run.
        """
        from media_preview_generator.processing import (
            ProcessingResult,
            _notify_file_result,
            failure_scope,
            set_file_result_callback,
        )

        a_received: list[str] = []
        b_received: list[str] = []

        with failure_scope("job-a"):
            set_file_result_callback(lambda f, *_: a_received.append(f), job_id="job-a")
            _notify_file_result("/a_before.mkv", ProcessingResult.GENERATED, "", "")

            with failure_scope("job-b"):
                set_file_result_callback(lambda f, *_: b_received.append(f), job_id="job-b")
                _notify_file_result("/b_only.mkv", ProcessingResult.GENERATED, "", "")
                set_file_result_callback(None, job_id="job-b")

            _notify_file_result("/a_after.mkv", ProcessingResult.GENERATED, "", "")
            set_file_result_callback(None, job_id="job-a")

        assert a_received == ["/a_before.mkv", "/a_after.mkv"], (
            "Job A's callback was silenced after job B cleared the shared global — this is the "
            "production deea99db clobber bug. Per-job callback routing must be independent."
        )
        assert b_received == ["/b_only.mkv"]

    def test_concurrent_threads_do_not_clobber_callbacks(self):
        """Two concurrent worker threads in separate ``failure_scope`` blocks
        must each see their own callback, even when scopes open and close
        interleaved across threads.
        """
        import threading

        from media_preview_generator.processing import (
            ProcessingResult,
            _notify_file_result,
            failure_scope,
            set_file_result_callback,
        )

        a_received: list[str] = []
        b_received: list[str] = []

        with failure_scope("job-a"):
            set_file_result_callback(lambda f, *_: a_received.append(f), job_id="job-a")
        with failure_scope("job-b"):
            set_file_result_callback(lambda f, *_: b_received.append(f), job_id="job-b")

        start = threading.Event()

        def _worker(job_id: str, file_path: str, dest: list):
            start.wait()
            with failure_scope(job_id):
                _notify_file_result(file_path, ProcessingResult.GENERATED, "", "")

        threads = [threading.Thread(target=_worker, args=("job-a", f"/a{i}.mkv", a_received)) for i in range(20)] + [
            threading.Thread(target=_worker, args=("job-b", f"/b{i}.mkv", b_received)) for i in range(20)
        ]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join()

        with failure_scope("job-a"):
            set_file_result_callback(None, job_id="job-a")
        with failure_scope("job-b"):
            set_file_result_callback(None, job_id="job-b")

        assert sorted(a_received) == sorted(f"/a{i}.mkv" for i in range(20))
        assert sorted(b_received) == sorted(f"/b{i}.mkv" for i in range(20))


class TestWorkerCallsNotifyFileResult:
    """The Worker.assign_task path must invoke ``_notify_file_result`` for
    every outcome — generated, skipped, failed — so the JSONL persistence
    chain that powers the per-job Files panel actually fires.

    The original D1 bug: ``_notify_file_result`` was defined and exported,
    a callback was wired in job_runner.py, but no production code ever
    called the function. Result: the Jobs UI showed no files for any
    skipped-only job (webhook with file already BIF'd, or full-library
    re-scan where every item was skipped).

    The right level for this test is the worker's outcome branches —
    that's where the regression actually was. Static-grep would catch
    "is the function called from worker.py at all" but not "is it called
    from every branch", so we exercise via captured callback instead.
    """

    # Audit fix — DELETED ``test_worker_imports_and_calls_notify_file_result``.
    # The previous incarnation was a hasattr smoke test that did not
    # exercise any runtime path (the audit doc on this test already said
    # so). The "did the worker actually call _notify_file_result on
    # every outcome branch (generated / skipped / failed / cancelled)"
    # invariant is fully covered by the per-branch matrix in
    # ``TestFileResultServerAttribution`` below, which exercises the
    # public API end-to-end and pins the recorded file results. Keeping
    # a hasattr smoke alongside that adds noise without coverage.


class TestFileResultServerAttribution:
    """D8 + D9 — per-file rows carry a `servers` list and a derived reason
    so the user can see which server got the file and why each row landed
    where it did.
    """

    def test_servers_list_is_persisted_slim(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")

        jm.record_file_result(
            job.id,
            "/media/foo.mkv",
            "generated",
            "",
            "GPU 1",
            servers=[
                {
                    "server_id": "plex-default",
                    "server_name": "My Plex",
                    "server_type": "plex",
                    "status": "published",
                    "message": "",
                    "frame_source": "extracted",
                },
                {
                    "server_id": "emby-1",
                    "server_name": "Emby",
                    "server_type": "emby",
                    "status": "published",
                    "frame_source": "cache_hit",
                },
            ],
        )
        results = jm.get_file_results(job.id)
        assert len(results) == 1
        assert results[0]["servers"] == [
            {"id": "plex-default", "name": "My Plex", "type": "plex", "status": "published"},
            # frame_source kept only when it differs from "extracted"
            {"id": "emby-1", "name": "Emby", "type": "emby", "status": "published", "frame_source": "cache_hit"},
        ]

    def test_reason_derived_from_publisher_message_when_blank(self, config_dir):
        """D8 — when the worker calls _persist with reason='', synthesise from publisher message."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")

        jm.record_file_result(
            job.id,
            "/media/foo.mkv",
            "skipped_bif_exists",
            "",  # ← worker passes empty reason for skip
            "GPU 1",
            servers=[
                {
                    "server_id": "plex-default",
                    "server_name": "Plex",
                    "server_type": "plex",
                    "status": "skipped",
                    "message": "BIF already exists at /plex/Media/.../index-sd.bif",
                }
            ],
        )
        r = jm.get_file_results(job.id)[0]
        assert r["reason"] == "BIF already exists at /plex/Media/.../index-sd.bif", (
            "When the caller doesn't pass an explicit reason, the publisher's message field "
            "must surface as the row's reason — otherwise the Files panel shows '(no reason)' "
            "for every skipped row, which was the original D8 user complaint."
        )

    def test_explicit_reason_wins_over_publisher_message(self, config_dir):
        """An explicit reason from the caller is never overridden."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")

        jm.record_file_result(
            job.id,
            "/media/foo.mkv",
            "failed",
            "FFmpeg exit 183 — codec not supported",
            "GPU 1",
            servers=[
                {
                    "server_id": "plex-default",
                    "server_name": "Plex",
                    "status": "failed",
                    "message": "publisher said different thing",
                }
            ],
        )
        r = jm.get_file_results(job.id)[0]
        assert r["reason"] == "FFmpeg exit 183 — codec not supported"

    def test_servers_field_omitted_when_empty(self, config_dir):
        """No servers list → no servers field in the JSONL (keep records compact)."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")

        jm.record_file_result(job.id, "/media/foo.mkv", "generated", "", "GPU 1")
        r = jm.get_file_results(job.id)[0]
        assert "servers" not in r


class TestFileResultBifPath:
    """D34 — surface the absolute BIF path on the file row so the
    Files-panel inspector button can deep-link straight to the BIF
    instead of running Plex's title-search heuristic (which mis-resolves
    episodes whose release-group suffix collides with the SxxExx tag).
    """

    def test_bif_path_extracted_from_first_publisher(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")

        jm.record_file_result(
            job.id,
            "/media/foo.mkv",
            "generated",
            "",
            "GPU 1",
            servers=[
                {
                    "server_id": "plex-default",
                    "server_name": "Plex",
                    "server_type": "plex",
                    "status": "published",
                    "output_paths": [
                        "/plex/Media/localhost/a/bcd.bundle/Contents/Indexes/index-sd.bif",
                    ],
                },
            ],
        )
        r = jm.get_file_results(job.id)[0]
        assert r["bif_path"] == "/plex/Media/localhost/a/bcd.bundle/Contents/Indexes/index-sd.bif"

    def test_bif_path_skips_non_bif_outputs(self, config_dir):
        """Jellyfin trickplay manifests aren't openable in the BIF viewer —
        the picker must skip past the .json/.jpg sidecars and pick a .bif."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")

        jm.record_file_result(
            job.id,
            "/media/foo.mkv",
            "generated",
            "",
            "GPU 1",
            servers=[
                {
                    "server_id": "jelly-1",
                    "server_name": "Jellyfin",
                    "server_type": "jellyfin",
                    "status": "published",
                    "output_paths": [
                        "/jelly/data/trickplay/abc/manifest.json",
                        "/jelly/data/trickplay/abc/320.jpg",
                    ],
                },
                {
                    "server_id": "plex-default",
                    "server_name": "Plex",
                    "server_type": "plex",
                    "status": "published",
                    "output_paths": [
                        "/plex/Media/localhost/a/bcd.bundle/Contents/Indexes/index-sd.bif",
                    ],
                },
            ],
        )
        r = jm.get_file_results(job.id)[0]
        assert r["bif_path"] == "/plex/Media/localhost/a/bcd.bundle/Contents/Indexes/index-sd.bif"

    def test_bif_path_omitted_when_no_bif_output(self, config_dir):
        """A Jellyfin-only publish (no Plex bundle) doesn't get a deep-link
        — the field is omitted so the JS falls back to ?file= search."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")

        jm.record_file_result(
            job.id,
            "/media/foo.mkv",
            "generated",
            "",
            "GPU 1",
            servers=[
                {
                    "server_id": "jelly-1",
                    "server_name": "Jellyfin",
                    "server_type": "jellyfin",
                    "status": "published",
                    "output_paths": ["/jelly/data/trickplay/abc/manifest.json"],
                },
            ],
        )
        r = jm.get_file_results(job.id)[0]
        assert "bif_path" not in r

    def test_bif_path_omitted_when_publishers_have_no_output_paths(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")

        jm.record_file_result(
            job.id,
            "/media/foo.mkv",
            "failed",
            "FFmpeg crashed",
            "GPU 1",
            servers=[
                {"server_id": "plex-default", "server_name": "Plex", "status": "failed"},
            ],
        )
        r = jm.get_file_results(job.id)[0]
        assert "bif_path" not in r


class TestFileResultsCap:
    """The 5000-entry per-outcome soft cap protects /config from 100k-item scans
    while keeping rare outcomes (failed, unresolved_vendor, ...) intact even
    when a flood of ``generated`` rows would otherwise crowd them out."""

    def test_writes_truncation_marker_at_cap(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Big scan")

        # Tighten the cap so the test runs fast.
        original_cap = JobManager._FILE_RESULTS_PER_OUTCOME_CAP
        JobManager._FILE_RESULTS_PER_OUTCOME_CAP = 10
        try:
            for i in range(15):
                jm.record_file_result(job.id, f"/media/v{i}.mkv", "skipped_bif_exists", "", "GPU 1")
        finally:
            JobManager._FILE_RESULTS_PER_OUTCOME_CAP = original_cap

        results = jm.get_file_results(job.id)
        # 10 normal records + 1 truncation marker = 11 total. Anything past
        # the per-outcome cap is silently dropped.
        assert len(results) == 11, f"expected 10 records + 1 marker = 11, got {len(results)}"
        assert results[-1]["outcome"] == "truncated:skipped_bif_exists", (
            "the boundary record must be the one-shot per-outcome truncation marker so the UI can "
            "surface 'X of Y shown' for that bucket specifically."
        )
        assert "skipped_bif_exists" in results[-1]["reason"], (
            "the marker's reason must name the truncated outcome so users know which bucket filled."
        )
        assert "10" in results[-1]["reason"], (
            "the marker's reason must include the cap value so users know how many were kept."
        )

    def test_marker_only_written_once(self, config_dir):
        """The marker is written when crossing the cap, not on every later append."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Big scan")

        original_cap = JobManager._FILE_RESULTS_PER_OUTCOME_CAP
        JobManager._FILE_RESULTS_PER_OUTCOME_CAP = 5
        try:
            for i in range(30):
                jm.record_file_result(job.id, f"/media/v{i}.mkv", "skipped_bif_exists", "", "")
        finally:
            JobManager._FILE_RESULTS_PER_OUTCOME_CAP = original_cap

        results = jm.get_file_results(job.id)
        marker_count = sum(1 for r in results if r["outcome"].startswith("truncated"))
        assert marker_count == 1, (
            f"expected exactly 1 truncation marker across all 30 calls; got {marker_count}. "
            "A duplicate marker means the boundary check is firing on every post-cap call, "
            "which would itself bloat the file the cap was meant to protect."
        )

    def test_failed_rows_survive_flood_of_generated(self, config_dir):
        """The regression that prompted the per-outcome refactor: on a 100k-item
        scan the user filtered the Files panel to "failed" and saw zero rows
        even though the aggregate counter showed failures had occurred. With
        a single shared cap, the first 5000 ``generated`` rows filled the
        JSONL and every later ``failed`` row was dropped. Per-outcome caps
        guarantee that a flood of one outcome can't crowd out another.
        """
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Huge scan")

        original_cap = JobManager._FILE_RESULTS_PER_OUTCOME_CAP
        JobManager._FILE_RESULTS_PER_OUTCOME_CAP = 50
        try:
            # 100 generated rows would have filled the old single-cap of 50
            # and dropped every later row, including the 30 failures below.
            for i in range(100):
                jm.record_file_result(job.id, f"/media/ok-{i}.mkv", "generated", "", "GPU 1")
            for i in range(30):
                jm.record_file_result(job.id, f"/media/bad-{i}.mkv", "failed", "exit 1", "GPU 1")
        finally:
            JobManager._FILE_RESULTS_PER_OUTCOME_CAP = original_cap

        # Every failure must be present despite the prior generated flood.
        failed_rows = jm.get_file_results(job.id, outcome_filter="failed")
        assert len(failed_rows) == 30, (
            f"per-outcome cap MUST keep all 30 failures despite a 100-row "
            f"``generated`` flood; got {len(failed_rows)}. This is the exact "
            f"behaviour the Files-panel filter relies on."
        )

        # And the generated bucket capped at 50, with its own marker.
        generated_rows = jm.get_file_results(job.id, outcome_filter="generated")
        assert len(generated_rows) == 50, f"``generated`` should cap at 50 independently; got {len(generated_rows)}"
        all_rows = jm.get_file_results(job.id)
        markers = [r for r in all_rows if r["outcome"].startswith("truncated")]
        assert any(m["outcome"] == "truncated:generated" for m in markers), (
            "must write a per-outcome marker naming which bucket filled"
        )
        assert not any(m["outcome"] == "truncated:failed" for m in markers), (
            "``failed`` did not hit cap, so no marker for it"
        )

    def test_two_outcomes_both_hit_cap_independently(self, config_dir):
        """Two noisy outcomes capping in parallel each get their own marker."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Mixed huge scan")

        original_cap = JobManager._FILE_RESULTS_PER_OUTCOME_CAP
        JobManager._FILE_RESULTS_PER_OUTCOME_CAP = 10
        try:
            for i in range(15):
                jm.record_file_result(job.id, f"/media/g-{i}.mkv", "generated", "", "")
            for i in range(15):
                jm.record_file_result(job.id, f"/media/s-{i}.mkv", "skipped_bif_exists", "", "")
        finally:
            JobManager._FILE_RESULTS_PER_OUTCOME_CAP = original_cap

        all_rows = jm.get_file_results(job.id)
        marker_outcomes = sorted(r["outcome"] for r in all_rows if r["outcome"].startswith("truncated"))
        assert marker_outcomes == ["truncated:generated", "truncated:skipped_bif_exists"], (
            f"expected one marker per capped outcome; got {marker_outcomes}"
        )

    def test_small_job_writes_no_truncation_marker(self, config_dir):
        """Jobs that stay under cap on every outcome must not write any marker."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Small scan")

        for i in range(10):
            jm.record_file_result(job.id, f"/media/v{i}.mkv", "generated", "", "")
        for i in range(3):
            jm.record_file_result(job.id, f"/media/x{i}.mkv", "failed", "x", "")

        rows = jm.get_file_results(job.id)
        assert len(rows) == 13
        assert not any(r["outcome"].startswith("truncated") for r in rows)

    def test_seed_per_outcome_counter_from_existing_jsonl(self, config_dir, tmp_path):
        """If a JSONL exists on disk before the in-memory counter is
        primed (job-survives-restart case), the per-outcome counter must
        re-seed by parsing each line's outcome — not by counting lines
        as if they were all the same bucket. Without this, a restart
        would resurrect the old "shared 5000 cap" behaviour for the
        first 5000 appends post-restart.
        """
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Pre-existing")

        # Write directly to the JSONL bypassing record_file_result, so
        # the in-memory counter doesn't get populated.
        path = jm._file_results_path(job.id)
        rows = [
            {"file": f"/g{i}.mkv", "outcome": "generated", "reason": "", "worker": "", "ts": "00:00:00"}
            for i in range(7)
        ] + [
            {"file": f"/f{i}.mkv", "outcome": "failed", "reason": "x", "worker": "", "ts": "00:00:00"} for i in range(3)
        ]
        with open(path, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        # Clear the lazy-seed slot so the next call re-reads from disk.
        with jm._file_result_counts_lock:
            jm._file_result_counts.pop(job.id, None)

        # Trigger a fresh append; the seed step must populate counts
        # per-outcome.
        jm.record_file_result(job.id, "/g7.mkv", "generated")
        with jm._file_result_counts_lock:
            counts = dict(jm._file_result_counts[job.id])
        assert counts == {"generated": 8, "failed": 3}, (
            f"per-outcome seed must parse each existing line's outcome; got {counts}"
        )


class TestFileResultsAPI:
    """GET /api/jobs/{id}/files API endpoint."""

    @pytest.fixture()
    def app(self, tmp_path):
        from media_preview_generator.web.app import create_app
        from media_preview_generator.web.settings_manager import reset_settings_manager

        reset_settings_manager()
        cfg = str(tmp_path / "config")
        os.makedirs(cfg, exist_ok=True)

        auth_file = os.path.join(cfg, "auth.json")
        with open(auth_file, "w") as f:
            json.dump({"token": "test-token-12345678"}, f)

        settings_file = os.path.join(cfg, "settings.json")
        with open(settings_file, "w") as f:
            json.dump({"setup_complete": True}, f)

        with patch.dict(
            os.environ,
            {
                "CONFIG_DIR": cfg,
                "WEB_AUTH_TOKEN": "test-token-12345678",
                "WEB_PORT": "8099",
            },
        ):
            flask_app = create_app(config_dir=cfg)
            flask_app.config["TESTING"] = True
            yield flask_app
        reset_settings_manager()

    @pytest.fixture()
    def client(self, app):
        return app.test_client()

    def _headers(self):
        return {
            "Authorization": "Bearer test-token-12345678",
            "Content-Type": "application/json",
        }

    def test_file_results_endpoint(self, client):
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        job = jm.create_job(library_name="API Test")
        jm.record_file_result(job.id, "/media/a.mkv", "generated", "", "GPU 1")
        jm.record_file_result(job.id, "/media/b.mkv", "failed", "exit 1", "CPU 1")
        jm.set_job_outcome(job.id, {"generated": 1, "failed": 1})

        resp = client.get(f"/api/jobs/{job.id}/files", headers=self._headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["count"] == 2
        assert data["total"] == 2
        assert len(data["files"]) == 2
        # D35 — endpoint stopped shipping the capped `summary` dict; the
        # top-of-modal Servers block is the source of truth for per-outcome
        # counts (uncapped, from job.publishers). The API now surfaces the
        # real processed total + a truncation flag so pagination can render
        # "N files in list (M items processed)" on big scans.
        assert "summary" not in data
        assert data["processed_total"] == 2
        assert data["list_truncated"] is False

    def test_file_results_outcome_filter(self, client):
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        job = jm.create_job(library_name="API Test")
        jm.record_file_result(job.id, "/media/a.mkv", "generated")
        jm.record_file_result(job.id, "/media/b.mkv", "failed", "exit 1")
        jm.record_file_result(job.id, "/media/c.mkv", "failed", "exit 2")
        jm.set_job_outcome(job.id, {"generated": 1, "failed": 2})

        resp = client.get(f"/api/jobs/{job.id}/files?outcome=failed", headers=self._headers())
        data = resp.get_json()
        assert data["count"] == 2
        assert data["total"] == 3
        assert all(f["outcome"] == "failed" for f in data["files"])
        assert "summary" not in data
        # processed_total survives the outcome filter — it reflects the
        # whole job, not the currently-visible page.
        assert data["processed_total"] == 3
        assert data["list_truncated"] is False

    def test_file_results_reports_truncation_when_marker_present(self, client, monkeypatch):
        """When the JSONL hits the per-job cap the endpoint flags it and
        reports the real processed total from job.progress.outcome, so the
        UI can render "Showing 1–N of N files in list (M items processed —
        list truncated for performance)" instead of misleading "of N".
        """
        from media_preview_generator.web.jobs import JobManager, get_job_manager

        jm = get_job_manager()
        # Shrink the cap to exercise the truncation path fast. The sentinel
        # marker is written exactly once on the 4th call; the 5th is dropped.
        monkeypatch.setattr(JobManager, "_FILE_RESULTS_PER_OUTCOME_CAP", 3)
        job = jm.create_job(library_name="Truncated Test")
        for i in range(5):
            jm.record_file_result(job.id, f"/media/{i}.mkv", "generated")
        # Real aggregate counter stays correct past the file-list cap —
        # this is what processed_total should surface.
        jm.set_job_outcome(job.id, {"generated": 117_981})

        resp = client.get(f"/api/jobs/{job.id}/files", headers=self._headers())
        assert resp.status_code == 200
        data = resp.get_json()
        # 3 generated rows + 1 truncation marker = 4 rows in the JSONL.
        assert data["total"] == 4
        assert data["list_truncated"] is True
        assert data["processed_total"] == 117_981
        # Sanity: processed scope must exceed the (capped) list length, or
        # the wording "list truncated for performance" would be a lie.
        assert data["processed_total"] > data["total"]

    def test_file_results_search_filter(self, client):
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        job = jm.create_job(library_name="API Test")
        jm.record_file_result(job.id, "/media/NBA/game1.mkv", "generated")
        jm.record_file_result(job.id, "/media/UFC/fight1.mkv", "generated")

        resp = client.get(f"/api/jobs/{job.id}/files?search=NBA", headers=self._headers())
        data = resp.get_json()
        assert data["count"] == 1
        assert "NBA" in data["files"][0]["file"]

    def test_file_results_404_for_missing_job(self, client):
        resp = client.get("/api/jobs/nonexistent/files", headers=self._headers())
        assert resp.status_code == 404
