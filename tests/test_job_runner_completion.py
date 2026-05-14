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
    _retry_completion_message,
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


class TestRetryCompletionMessage:
    """Cover the matrix that produces the per-job tail log line for retries.

    Live regression (2026-05-14, 15 failed jobs): every exhausted retry
    chain wrote ``INFO - Retry job completed successfully`` on the child
    job's tail even though the parent had just been marked FAILED with
    "Source server did not register N file(s) after 3 retry attempts".
    The contradiction made per-job logs unreliable for diagnosis.

    The branching variable is ``(retry_paths empty?, spawned_retry_id set?)``.
    Cell ``(non-empty, None)`` is chain-exhausted — the only cell that
    must NOT log success. The other live cells route through different
    code paths (``error_parts`` non-empty), but the helper still has to
    handle the scheduled-retry case for completeness.
    """

    def test_chain_exhausted_logs_warning_with_pending_summary(self):
        """retry_paths non-empty + no spawn = chain exhausted = WARNING."""
        level, msg = _retry_completion_message(
            retry_paths=["/data/x.mkv"],
            spawned_retry_id=None,
            pending_by_server={"JellyTest": 1},
            effective_max=3,
        )
        assert level == "WARNING", "exhausted chains must NOT log INFO success"
        assert "Retry chain exhausted after 3 attempt(s)" in msg
        assert "JellyTest pending × 1" in msg, f"per-server pending count must surface in the message; got {msg!r}"

    def test_chain_exhausted_orders_pending_servers_by_count(self):
        """Multi-server pending counts sort highest-first so the most-blocked
        server is named first — keeps the message scannable when 3+ servers
        are pending."""
        level, msg = _retry_completion_message(
            retry_paths=["/a.mkv", "/b.mkv", "/c.mkv"],
            spawned_retry_id=None,
            pending_by_server={"Plex": 1, "JellyTest": 3, "EmbyTest": 2},
            effective_max=3,
        )
        assert level == "WARNING"
        assert msg.index("JellyTest pending × 3") < msg.index("EmbyTest pending × 2") < msg.index("Plex pending × 1"), (
            f"pending servers must sort by count descending; got {msg!r}"
        )

    def test_chain_exhausted_falls_back_when_pending_by_server_empty(self):
        """If pending_by_server got cleared but retry_paths still has entries,
        the message must still carry a count rather than ending with a stray
        semicolon — exhausted-chain warnings always need *some* signal.
        """
        level, msg = _retry_completion_message(
            retry_paths=["/a.mkv", "/b.mkv"],
            spawned_retry_id=None,
            pending_by_server={},
            effective_max=3,
        )
        assert level == "WARNING"
        assert "2 path(s) still pending" in msg, f"empty pending_by_server must fall back to a path count; got {msg!r}"

    def test_retry_succeeded_logs_info_success(self):
        """retry_paths empty = chain succeeded = original INFO message preserved."""
        level, msg = _retry_completion_message(
            retry_paths=[],
            spawned_retry_id=None,
            pending_by_server={},
            effective_max=3,
        )
        assert level == "INFO"
        assert msg == "Retry job completed successfully"

    def test_spawned_next_retry_logs_info_success(self):
        """If we DID spawn another retry, this child completed its work and
        the chain is still alive — INFO is correct (the WARNING about the
        next-retry schedule is logged elsewhere, lines 1239-1242).
        """
        level, msg = _retry_completion_message(
            retry_paths=["/a.mkv"],
            spawned_retry_id="next-retry-job-id",
            pending_by_server={"JellyTest": 1},
            effective_max=3,
        )
        assert level == "INFO"
        assert msg == "Retry job completed successfully"
