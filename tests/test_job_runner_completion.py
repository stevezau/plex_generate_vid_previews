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

from types import SimpleNamespace

import pytest

from media_preview_generator.web.routes.job_runner import (
    _classify_job_completion,
    _format_retry_wait_server_label,
)


def _job(publishers=None, *, server_id=None, server_name=None, server_type=None, config=None):
    """Stand-in for a ``Job`` row: only the attributes the SUT reads."""
    return SimpleNamespace(
        publishers=publishers or [],
        server_id=server_id,
        server_name=server_name,
        server_type=server_type,
        config=config or {},
    )


def _pending_pub(server_name, server_type, count=1):
    return {
        "server_name": server_name,
        "server_type": server_type,
        "counts": {"published_pending_registration": count},
    }


class TestFormatRetryWaitServerLabel:
    """Covers the five branches that produce different retry-wait copy.

    The fix this guards (chain ``2f7132d5``, 2026-05-13) was a label-rendering
    regression where the source-pill server was conflated with the publish
    target. Each cell below produces visibly different copy — the matrix
    must be tested so the conflation can't silently come back.
    """

    def test_single_pending_publisher_uses_publisher_name(self):
        parent = _job(publishers=[_pending_pub("Jellyfin NAS", "jellyfin")])
        retry = _job(server_id="plex-main", server_name="Plex", server_type="plex")
        assert _format_retry_wait_server_label(parent, retry) == "Jellyfin NAS"

    def test_two_pending_publishers_use_and(self):
        parent = _job(
            publishers=[
                _pending_pub("Plex Living Room", "plex"),
                _pending_pub("Jellyfin NAS", "jellyfin"),
            ]
        )
        assert _format_retry_wait_server_label(parent, _job()) == "Plex Living Room and Jellyfin NAS"

    def test_three_plus_pending_publishers_use_oxford_comma(self):
        parent = _job(
            publishers=[
                _pending_pub("Plex", "plex"),
                _pending_pub("Emby", "emby"),
                _pending_pub("Jellyfin", "jellyfin"),
            ]
        )
        assert _format_retry_wait_server_label(parent, _job()) == "Plex, Emby, and Jellyfin"

    def test_pin_matches_source_when_no_publishers_data(self):
        """No parent publishers info, but an explicit publish-pin equal to
        the source attribution → name that server.
        """
        retry = _job(
            server_id="plex-main",
            server_name="Plex Main",
            server_type="plex",
            config={"server_id": "plex-main"},
        )
        assert _format_retry_wait_server_label(None, retry) == "Plex Main"

    def test_pin_differs_from_source_falls_back_to_generic(self):
        """Source attribution (top-level server_id) is Plex, but the explicit
        publish-pin is something else — naming the source would re-introduce
        the chain ``2f7132d5`` source-vs-target conflation, so we go generic.
        """
        retry = _job(
            server_id="plex-main",
            server_name="Plex",
            server_type="plex",
            config={"server_id": "jellyfin-nas"},
        )
        assert _format_retry_wait_server_label(None, retry) == "the media server"

    def test_no_publishers_no_pin_falls_back_to_generic(self):
        assert _format_retry_wait_server_label(None, _job()) == "the media server"

    @pytest.mark.parametrize(
        "publishers",
        [
            [{"server_name": "X", "counts": "not-a-dict"}],
            ["not-a-dict"],
            [{"server_name": "Y", "counts": {"some_other_status": 5}}],
        ],
    )
    def test_malformed_or_non_pending_publishers_dont_name_a_server(self, publishers):
        """Defensive: persisted ``publishers`` is JSON-text so a partial write
        or hand-edited row could surface non-dict entries or unfamiliar status
        keys. Those must collapse to the generic fallback, not raise.
        """
        assert _format_retry_wait_server_label(_job(publishers=publishers), _job()) == "the media server"


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
