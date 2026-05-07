"""Tests for the job-completion badge classification rule.

The classifier decides whether a finished job ends the run with a red
"Failed" badge or an amber "Completed with warnings" badge. The rule
used to start with ``bool(failures)`` — any single FFmpeg crash flipped
the badge red regardless of how many items succeeded. On the 128k-item
scan job ``deea99db`` this meant 1 failure out of 128000 successes
rendered as "Failed", which misrepresented the run.

The fix: only treat the run as a hard failure when nothing succeeded.
Partial-failure jobs drop to the warning branch the code already had.
"""

from __future__ import annotations

from media_preview_generator.web.routes.job_runner import _classify_job_completion


class TestClassifyJobCompletion:
    def test_single_ffmpeg_crash_amid_128k_successes_is_warning_not_failure(self):
        """Production incident deea99db: 128000 succeeded, 1 FFmpeg crash,
        1 silent publisher failure. Users saw a red badge and assumed the
        whole run broke. Anything with even one successful publish is a
        warning, never a hard failure.
        """
        result = _classify_job_completion(
            failures=[{"file": "/anime.mkv", "exit_code": 218, "reason": "x"}],
            outcome={
                "generated": 1,
                "skipped_output_exists": 127999,
                "failed": 2,
            },
            is_retry=False,
            retry_paths=[],
            spawned_retry_id=None,
            total_paths=0,
            resolved_count=0,
        )
        assert result == "warning", (
            f"1-in-128k FFmpeg crash must not flip the badge red when 128000 items succeeded; "
            f"got {result!r}. This was the deea99db regression."
        )

    def test_every_item_failed_is_hard_failure(self):
        """When nothing succeeded, the badge must be red — the all-failed
        gate is what the user relies on to see "go fix this now".
        """
        result = _classify_job_completion(
            failures=[{"file": "/a.mkv", "exit_code": 1, "reason": "x"}],
            outcome={"failed": 10},
            is_retry=False,
            retry_paths=[],
            spawned_retry_id=None,
            total_paths=0,
            resolved_count=0,
        )
        assert result == "error"

    def test_retry_job_with_leftover_paths_is_hard_failure(self):
        """A retry job that exits with unresolved retry paths (and didn't
        spawn another retry) is a terminal failure — the user's webhook
        never got served.
        """
        result = _classify_job_completion(
            failures=[],
            outcome={"generated": 1},
            is_retry=True,
            retry_paths=["/still-missing.mkv"],
            spawned_retry_id=None,
            total_paths=1,
            resolved_count=1,
        )
        assert result == "error"

    def test_all_not_found_without_spawn_is_hard_failure(self):
        """Every file Plex resolved was missing on disk AND no retry was
        scheduled: nothing more is going to happen, so surface as a hard
        failure rather than letting it look green-with-a-footnote.
        """
        result = _classify_job_completion(
            failures=[],
            outcome={"generated": 0, "skipped_file_not_found": 5},
            is_retry=False,
            retry_paths=[],
            spawned_retry_id=None,
            total_paths=5,
            resolved_count=5,
        )
        assert result == "error"

    def test_nothing_resolved_is_hard_failure(self):
        """The user submitted paths but none resolved against Plex — the
        job did nothing, and the badge must reflect that.
        """
        result = _classify_job_completion(
            failures=[],
            outcome={},
            is_retry=False,
            retry_paths=[],
            spawned_retry_id=None,
            total_paths=3,
            resolved_count=0,
        )
        assert result == "error"

    def test_partial_failure_multi_server_scan_is_warning(self):
        """A multi-server full scan with some ``published`` /
        ``skipped_output_exists`` successes and some ``failed`` items
        is an amber warning — the successful ones are real work.
        """
        result = _classify_job_completion(
            failures=[],
            outcome={"published": 50, "skipped_output_exists": 40, "failed": 10},
            is_retry=False,
            retry_paths=[],
            spawned_retry_id=None,
            total_paths=0,
            resolved_count=0,
        )
        assert result == "warning"

    def test_no_owners_alone_is_warning_not_error(self):
        """A ``no_owners`` outcome means the files are outside any
        configured library — that's a config hint, not a broken run.
        """
        result = _classify_job_completion(
            failures=[],
            outcome={"published": 5, "no_owners": 2},
            is_retry=False,
            retry_paths=[],
            spawned_retry_id=None,
            total_paths=0,
            resolved_count=0,
        )
        assert result == "warning"
