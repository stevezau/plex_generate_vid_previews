"""
Tests for ETA-related behavior after removal of job-level ETA.

Job-level ETA was removed; worker ETA (from ffmpeg) is still exposed via WorkerStatus.eta.
This module tests the data model and formatting contract for worker ETA.
"""

from plex_generate_previews.web.jobs import JobProgress, WorkerStatus


def _format_eta(seconds: float) -> str:
    """Mirror of the _format_eta used in routes for worker ETA (ffmpeg remaining_time)."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    else:
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"


class TestJobProgressNoEta:
    """JobProgress no longer has an eta field (job-level ETA removed)."""

    def test_job_progress_default_has_no_eta(self):
        p = JobProgress()
        assert not hasattr(p, "eta")
        d = p.to_dict()
        assert "eta" not in d

    def test_job_progress_to_dict_omits_eta(self):
        p = JobProgress(percent=50.0, processed_items=10, total_items=20)
        d = p.to_dict()
        assert "eta" not in d


class TestWorkerStatusEta:
    """WorkerStatus retains eta for ffmpeg-based per-worker ETA."""

    def test_worker_status_has_eta(self):
        w = WorkerStatus(eta="2m 5s")
        assert w.eta == "2m 5s"
        assert w.to_dict()["eta"] == "2m 5s"

    def test_worker_status_eta_default_empty(self):
        w = WorkerStatus()
        assert w.eta == ""


class TestFormatEtaWorkerDisplay:
    """Format used for worker ETA display (seconds -> human-readable)."""

    def test_seconds_only(self):
        assert _format_eta(45) == "45s"

    def test_minutes_and_seconds(self):
        assert _format_eta(125) == "2m 5s"

    def test_hours_and_minutes(self):
        assert _format_eta(3700) == "1h 1m"

    def test_zero(self):
        assert _format_eta(0) == "0s"
