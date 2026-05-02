"""Tests for the slow-backoff retry queue."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.processing.retry_queue import (
    _BACKOFF,
    RetryScheduler,
    get_retry_scheduler,
    reset_retry_scheduler,
    schedule_retry_for_unindexed,
)
from tests.conftest import _ms, _pi  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Drop the process-wide retry scheduler between tests so timers don't bleed."""
    reset_retry_scheduler()
    yield
    reset_retry_scheduler()


class TestBackoffSchedule:
    def test_schedule_grows_geometrically(self):
        # 30s → 2m → 5m → 15m → 60m. Each gap should be larger than the
        # previous one — protects against accidental re-tuning to a flat
        # schedule.
        for prev, nxt in zip(_BACKOFF, _BACKOFF[1:], strict=False):
            assert nxt > prev, _BACKOFF

    def test_first_delay_under_a_minute(self):
        """First retry must fire fast — Plex scans usually complete in seconds."""
        assert _BACKOFF[0] <= 60

    def test_public_alias_is_same_object(self):
        """D15 — BACKOFF_SCHEDULE is the public name; _BACKOFF is the
        backwards-compat alias. The job_runner spawn-retry path imports
        the public name so the resolution-step retry cadence stays in
        lockstep with the publisher-step retry cadence."""
        from media_preview_generator.processing.retry_queue import BACKOFF_SCHEDULE

        assert BACKOFF_SCHEDULE is _BACKOFF
        assert BACKOFF_SCHEDULE == (30, 120, 300, 900, 3600)


@pytest.mark.slow
class TestRetrySchedulerSchedule:
    def test_schedule_fires_callback_after_delay(self):
        sched = RetryScheduler()
        ran = threading.Event()
        captured: list[tuple[str, int]] = []

        def cb(path, attempt):
            captured.append((path, attempt))
            ran.set()

        # Patch _BACKOFF to use 0.05s for fast tests.
        with patch(
            "media_preview_generator.processing.retry_queue._BACKOFF",
            (0.05, 0.05, 0.05),
        ):
            assert sched.schedule("/canonical", cb, attempt=1) is True
            assert sched.pending_count() == 1
            assert ran.wait(timeout=2)

        assert captured == [("/canonical", 1)]
        # State is cleaned up after firing.
        assert sched.pending_count() == 0

    def test_schedule_replaces_existing_timer_for_same_path(self):
        sched = RetryScheduler()
        first_callback_calls = 0
        second_callback_calls = 0

        def first_cb(path, attempt):
            nonlocal first_callback_calls
            first_callback_calls += 1

        def second_cb(path, attempt):
            nonlocal second_callback_calls
            second_callback_calls += 1

        with patch(
            "media_preview_generator.processing.retry_queue._BACKOFF",
            (0.5, 0.5, 0.5),
        ):
            sched.schedule("/canonical", first_cb, attempt=1)
            sched.schedule("/canonical", second_cb, attempt=1)
            time.sleep(0.8)

        assert first_callback_calls == 0, "old timer should have been cancelled"
        assert second_callback_calls == 1

    def test_schedule_returns_false_after_max_attempts(self):
        """attempt > len(_BACKOFF) → give up, return False."""
        sched = RetryScheduler()

        def cb(path, attempt):
            pass

        attempts_attempted = list(range(1, len(_BACKOFF) + 2))
        with patch(
            "media_preview_generator.processing.retry_queue._BACKOFF",
            tuple([60] * len(_BACKOFF)),
        ):
            for n in attempts_attempted:
                ok = sched.schedule(f"/{n}", cb, attempt=n)
                if n <= len(_BACKOFF):
                    assert ok is True, n
                else:
                    assert ok is False, n


@pytest.mark.slow
class TestRetrySchedulerCancel:
    def test_cancel_pending_retry(self):
        sched = RetryScheduler()
        cb_calls = 0

        def cb(path, attempt):
            nonlocal cb_calls
            cb_calls += 1

        with patch(
            "media_preview_generator.processing.retry_queue._BACKOFF",
            (0.5, 0.5, 0.5),
        ):
            sched.schedule("/x", cb, attempt=1)
            assert sched.cancel("/x") is True
            time.sleep(0.6)

        assert cb_calls == 0
        assert sched.pending_count() == 0

    def test_cancel_returns_false_when_nothing_pending(self):
        sched = RetryScheduler()
        assert sched.cancel("/never-scheduled") is False


@pytest.mark.slow
class TestSchedulerSingleton:
    def test_get_retry_scheduler_is_singleton(self):
        a = get_retry_scheduler()
        b = get_retry_scheduler()
        assert a is b

    def test_reset_drops_singleton_and_cancels_pending(self):
        sched = get_retry_scheduler()
        cb_calls = 0

        def cb(path, attempt):
            nonlocal cb_calls
            cb_calls += 1

        with patch(
            "media_preview_generator.processing.retry_queue._BACKOFF",
            (0.5,) + tuple([0.5] * (len(_BACKOFF) - 1)),
        ):
            sched.schedule("/x", cb, attempt=1)
            reset_retry_scheduler()
            time.sleep(0.7)

        assert cb_calls == 0


@pytest.mark.slow
class TestScheduleRetryForUnindexed:
    """The integration-y wrapper that calls back into process_canonical_path."""

    def test_callback_invokes_process_canonical_path(self):
        """When the timer fires, it calls process_canonical_path with our args."""
        registry = MagicMock(name="registry")
        config = MagicMock(name="config")
        captured: list[dict] = []
        ran = threading.Event()

        def fake_process(**kwargs):
            captured.append(kwargs)
            ran.set()
            # Return a result with no SKIPPED_NOT_INDEXED so chain ends.
            from media_preview_generator.processing.multi_server import (
                MultiServerResult,
                MultiServerStatus,
            )

            return MultiServerResult(
                canonical_path=kwargs["canonical_path"],
                status=MultiServerStatus.PUBLISHED,
                publishers=[],
                frame_count=0,
                message="ok",
            )

        with (
            patch(
                "media_preview_generator.processing.retry_queue._BACKOFF",
                (0.05,) + tuple([0.5] * (len(_BACKOFF) - 1)),
            ),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=fake_process,
            ),
        ):
            ok = schedule_retry_for_unindexed(
                "/canonical/foo.mkv",
                registry=registry,
                config=config,
                item_id_by_server={"plex-1": "abc"},
                attempt=1,
            )
            assert ok is True
            assert ran.wait(timeout=2)

        assert len(captured) == 1
        assert captured[0]["canonical_path"] == "/canonical/foo.mkv"
        assert captured[0]["item_id_by_server"] == {"plex-1": "abc"}
        assert captured[0]["registry"] is registry
        # The retry callback must opt out of further auto-scheduling so
        # we don't get a runaway timer fork bomb.
        assert captured[0]["schedule_retry_on_not_indexed"] is False

    def test_chained_retry_fires_when_still_unindexed(self):
        """Retry that returns SKIPPED_NOT_INDEXED schedules a follow-up."""
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
            PublisherResult,
            PublisherStatus,
        )

        registry = MagicMock()
        config = MagicMock()

        # First retry call: still not indexed. Second retry: published.
        call_count = {"n": 0}
        chain_complete = threading.Event()

        def fake_process(**kwargs):
            call_count["n"] += 1
            if call_count["n"] < 2:
                return MultiServerResult(
                    canonical_path=kwargs["canonical_path"],
                    status=MultiServerStatus.SKIPPED,
                    publishers=[
                        PublisherResult(
                            server_id="plex-1",
                            server_name="plex-1",
                            adapter_name="plex_bundle",
                            status=PublisherStatus.SKIPPED_NOT_INDEXED,
                            message="not yet",
                        )
                    ],
                    frame_count=0,
                    message="waiting",
                )
            chain_complete.set()
            return MultiServerResult(
                canonical_path=kwargs["canonical_path"],
                status=MultiServerStatus.PUBLISHED,
                publishers=[
                    PublisherResult(
                        server_id="plex-1",
                        server_name="plex-1",
                        adapter_name="plex_bundle",
                        status=PublisherStatus.PUBLISHED,
                        message="ok",
                    )
                ],
                frame_count=6,
                message="ok",
            )

        with (
            patch(
                "media_preview_generator.processing.retry_queue._BACKOFF",
                (0.05,) + tuple([0.05] * (len(_BACKOFF) - 1)),
            ),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=fake_process,
            ),
        ):
            schedule_retry_for_unindexed(
                "/foo.mkv",
                registry=registry,
                config=config,
                item_id_by_server=None,
                attempt=1,
            )
            assert chain_complete.wait(timeout=2)
            time.sleep(0.05)  # let the chain-complete log run

        assert call_count["n"] == 2

    def test_chain_terminates_after_max_attempts(self):
        """If every retry returns SKIPPED_NOT_INDEXED, give up after _BACKOFF length."""
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
            PublisherResult,
            PublisherStatus,
        )

        registry = MagicMock()
        config = MagicMock()
        call_count = {"n": 0}

        def fake_process(**kwargs):
            call_count["n"] += 1
            return MultiServerResult(
                canonical_path=kwargs["canonical_path"],
                status=MultiServerStatus.SKIPPED,
                publishers=[
                    PublisherResult(
                        server_id="plex-1",
                        server_name="plex-1",
                        adapter_name="plex_bundle",
                        status=PublisherStatus.SKIPPED_NOT_INDEXED,
                        message="not yet",
                    )
                ],
                frame_count=0,
                message="waiting",
            )

        with (
            patch(
                "media_preview_generator.processing.retry_queue._BACKOFF",
                tuple([0.02] * len(_BACKOFF)),
            ),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=fake_process,
            ),
        ):
            schedule_retry_for_unindexed(
                "/x",
                registry=registry,
                config=config,
                attempt=1,
            )
            # Wait for full chain: len(_BACKOFF) attempts × 0.02s, plus padding.
            time.sleep(0.05 * len(_BACKOFF) + 0.5)

        assert call_count["n"] == len(_BACKOFF), f"expected exactly {len(_BACKOFF)} retries, got {call_count['n']}"

    def test_callback_swallows_exceptions(self):
        """If process_canonical_path raises, the timer thread doesn't die."""
        registry = MagicMock()
        config = MagicMock()
        ran = threading.Event()

        def boom(**kwargs):
            ran.set()
            raise RuntimeError("dispatch broke")

        # _BACKOFF length 1: when boom raises and _callback's except branch
        # tries to re-schedule with attempt=2, that exceeds the chain so
        # schedule_retry_for_unindexed returns False without queuing a
        # follow-up timer. Without this, the leaked timer fires during
        # the next test and pollutes its schedule_retry_for_unindexed spy.
        with (
            patch(
                "media_preview_generator.processing.retry_queue._BACKOFF",
                (0.02,),
            ),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=boom,
            ),
        ):
            schedule_retry_for_unindexed(
                "/x",
                registry=registry,
                config=config,
                attempt=1,
            )
            assert ran.wait(timeout=2)
            # Give the timer thread a moment to finish its except branch +
            # reach the give-up log line so the singleton is fully drained
            # before teardown runs reset_retry_scheduler.
            time.sleep(0.1)

        # Test would crash if the timer thread had propagated the
        # exception up and killed the process; reaching here means we
        # caught + logged it.


@pytest.fixture
def _isolated_frame_cache():
    """Reset the frame-cache singleton around each test so consecutive
    tests can use different ``base_dir`` values without tripping the
    "already initialised" guard."""
    from media_preview_generator.processing.frame_cache import reset_frame_cache

    reset_frame_cache()
    yield
    reset_frame_cache()


class TestProcessCanonicalPathIntegration:
    """process_canonical_path schedules a retry when SKIPPED_NOT_INDEXED happens.

    These tests exercise the *real* ``_publish_one`` path: instead of
    stubbing the same-module helper (the D31-style anti-pattern), we make
    the adapter's ``compute_output_paths`` raise
    :class:`LibraryNotYetIndexedError`, which is exactly how the production
    "not yet indexed" signal reaches ``_publish_one`` in normal operation
    (Plex's bundle-hash lookup hasn't populated yet).
    """

    def _make_adapter(self, raise_not_indexed: bool):
        from media_preview_generator.servers.base import LibraryNotYetIndexedError

        adapter = MagicMock()
        adapter.name = "plex_bundle"
        if raise_not_indexed:
            adapter.compute_output_paths.side_effect = LibraryNotYetIndexedError("bundle hash unavailable")
        else:
            adapter.compute_output_paths.return_value = ["/tmp/out/sd.bif"]
        return adapter

    def test_skipped_not_indexed_triggers_retry_schedule(self, _isolated_frame_cache):
        """When the only publisher comes back SKIPPED_NOT_INDEXED, dispatcher
        must schedule one retry through the retry queue boundary.

        The boundary we mock is the retry-queue helper itself
        (``schedule_retry_for_unindexed``) so we can read off the keyword
        arguments the dispatcher actually passed — including the canonical
        path it preserved end-to-end. Spying here (vs at ``_publish_one``)
        keeps the production ``_publish_one`` body covered, which is what
        the D31 audit requires.
        """
        registry = MagicMock()
        server = MagicMock(id="plex-1", name="plex-1")
        adapter = self._make_adapter(raise_not_indexed=True)

        config = MagicMock()
        config.working_tmp_folder = "/tmp/x"
        config.plex_bif_frame_interval = 5
        config.thumbnail_interval = 5

        schedule_calls: list[dict] = []

        def _spy_schedule(*args, **kwargs):
            schedule_calls.append({"args": args, "kwargs": kwargs})
            return True

        with (
            patch(
                "media_preview_generator.processing.multi_server._resolve_publishers",
                return_value=[(server, adapter, "rk-1")],
            ),
            patch(
                "media_preview_generator.processing.multi_server._resolve_item_id_for",
                return_value="rk-1",
            ),
            patch(
                "media_preview_generator.processing.multi_server.os.path.isfile",
                return_value=True,
            ),
            patch(
                "media_preview_generator.processing.multi_server.generate_images",
                return_value=(True, 6, "h264", 1.0, 30.0, 320),
            ),
            patch(
                "media_preview_generator.processing.multi_server.os.makedirs",
            ),
            patch(
                "media_preview_generator.processing.multi_server.os.listdir",
                return_value=["00001.jpg"] * 6,
            ),
            patch(
                "media_preview_generator.processing.retry_queue.schedule_retry_for_unindexed",
                side_effect=_spy_schedule,
            ),
        ):
            from media_preview_generator.processing.multi_server import (
                MultiServerStatus,
                process_canonical_path,
            )

            result = process_canonical_path(
                canonical_path="/x.mkv",
                registry=registry,
                config=config,
                use_frame_cache=False,
            )

        # D13: every publisher was SKIPPED_NOT_INDEXED → aggregate is the
        # dedicated bucket, not generic SKIPPED.
        from media_preview_generator.processing.multi_server import MultiServerStatus

        assert result.status is MultiServerStatus.SKIPPED_NOT_INDEXED
        # _publish_one really ran: it caught LibraryNotYetIndexedError and
        # produced exactly the SKIPPED_NOT_INDEXED publisher result.
        assert len(result.publishers) == 1
        assert result.publishers[0].adapter_name == "plex_bundle"
        # Exactly one retry got scheduled, with the canonical path the
        # dispatcher started from and attempt counter incremented from 0 → 1.
        assert len(schedule_calls) == 1, schedule_calls
        kw = schedule_calls[0]["kwargs"]
        args = schedule_calls[0]["args"]
        # canonical_path is positional in the production call site.
        assert args and args[0] == "/x.mkv", schedule_calls
        assert kw.get("attempt") == 1

    def test_skipped_not_indexed_no_retry_when_disabled(self, _isolated_frame_cache):
        """schedule_retry_on_not_indexed=False suppresses scheduling.

        Same boundary: real ``_publish_one`` runs, only the retry-queue
        helper is spied on so we can prove zero invocations.
        """
        registry = MagicMock()
        server = MagicMock(id="plex-1", name="plex-1")
        adapter = self._make_adapter(raise_not_indexed=True)

        config = MagicMock()
        config.working_tmp_folder = "/tmp/x"
        config.plex_bif_frame_interval = 5
        config.thumbnail_interval = 5

        schedule_calls: list[dict] = []

        def _spy_schedule(*args, **kwargs):
            schedule_calls.append({"args": args, "kwargs": kwargs})
            return True

        with (
            patch(
                "media_preview_generator.processing.multi_server._resolve_publishers",
                return_value=[(server, adapter, "rk-1")],
            ),
            patch(
                "media_preview_generator.processing.multi_server._resolve_item_id_for",
                return_value="rk-1",
            ),
            patch(
                "media_preview_generator.processing.multi_server.os.path.isfile",
                return_value=True,
            ),
            patch(
                "media_preview_generator.processing.multi_server.generate_images",
                return_value=(True, 6, "h264", 1.0, 30.0, 320),
            ),
            patch(
                "media_preview_generator.processing.multi_server.os.makedirs",
            ),
            patch(
                "media_preview_generator.processing.multi_server.os.listdir",
                return_value=["00001.jpg"] * 6,
            ),
            patch(
                "media_preview_generator.processing.retry_queue.schedule_retry_for_unindexed",
                side_effect=_spy_schedule,
            ),
        ):
            from media_preview_generator.processing.multi_server import process_canonical_path

            process_canonical_path(
                canonical_path="/x.mkv",
                registry=registry,
                config=config,
                use_frame_cache=False,
                schedule_retry_on_not_indexed=False,
            )

        assert schedule_calls == []
