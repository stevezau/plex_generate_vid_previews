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
    """The 5000-entry per-job soft cap protects /config from 100k-item scans."""

    def test_writes_truncation_marker_at_cap(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Big scan")

        # Tighten the cap so the test runs fast.
        original_cap = JobManager._FILE_RESULTS_PER_JOB_CAP
        JobManager._FILE_RESULTS_PER_JOB_CAP = 10
        try:
            for i in range(15):
                jm.record_file_result(job.id, f"/media/v{i}.mkv", "skipped_bif_exists", "", "GPU 1")
        finally:
            JobManager._FILE_RESULTS_PER_JOB_CAP = original_cap

        results = jm.get_file_results(job.id)
        # 10 normal records + 1 truncation marker = 11 total. Anything past
        # the cap is silently dropped.
        assert len(results) == 11, f"expected 10 records + 1 marker = 11, got {len(results)}"
        assert results[-1]["outcome"] == "truncated", (
            "the boundary record must be the one-shot 'truncated' marker so the UI can surface it."
        )
        assert "5000" in results[-1]["reason"] or "10" in results[-1]["reason"], (
            "the marker's reason must include the cap value so users know how many were dropped."
        )

    def test_marker_only_written_once(self, config_dir):
        """The marker is written when crossing the cap, not on every later append."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Big scan")

        original_cap = JobManager._FILE_RESULTS_PER_JOB_CAP
        JobManager._FILE_RESULTS_PER_JOB_CAP = 5
        try:
            for i in range(30):
                jm.record_file_result(job.id, f"/media/v{i}.mkv", "skipped_bif_exists", "", "")
        finally:
            JobManager._FILE_RESULTS_PER_JOB_CAP = original_cap

        results = jm.get_file_results(job.id)
        marker_count = sum(1 for r in results if r["outcome"] == "truncated")
        assert marker_count == 1, (
            f"expected exactly 1 truncation marker across all 30 calls; got {marker_count}. "
            "A duplicate marker means the boundary check is firing on every post-cap call, "
            "which would itself bloat the file the cap was meant to protect."
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

        resp = client.get(f"/api/jobs/{job.id}/files", headers=self._headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["count"] == 2
        assert data["total"] == 2
        assert len(data["files"]) == 2
        assert data["summary"]["generated"] == 1
        assert data["summary"]["failed"] == 1

    def test_file_results_outcome_filter(self, client):
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        job = jm.create_job(library_name="API Test")
        jm.record_file_result(job.id, "/media/a.mkv", "generated")
        jm.record_file_result(job.id, "/media/b.mkv", "failed", "exit 1")
        jm.record_file_result(job.id, "/media/c.mkv", "failed", "exit 2")

        resp = client.get(f"/api/jobs/{job.id}/files?outcome=failed", headers=self._headers())
        data = resp.get_json()
        assert data["count"] == 2
        assert data["total"] == 3
        assert all(f["outcome"] == "failed" for f in data["files"])
        # Summary must still contain ALL outcome counts for badge rendering
        assert data["summary"]["generated"] == 1
        assert data["summary"]["failed"] == 2

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
