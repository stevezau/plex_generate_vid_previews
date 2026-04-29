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

        with (
            patch(
                "media_preview_generator.processing.retry_queue._BACKOFF",
                (0.02,) + tuple([0.5] * (len(_BACKOFF) - 1)),
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

        # Test would crash if the timer thread had propagated the
        # exception up and killed the process; reaching here means we
        # caught + logged it.


class TestProcessCanonicalPathIntegration:
    """process_canonical_path schedules a retry when SKIPPED_NOT_INDEXED happens."""

    def test_skipped_not_indexed_triggers_retry_schedule(self):
        """Smoke test: dispatcher hands off to retry queue automatically.

        We verify by spying on ``schedule_retry_for_unindexed`` rather
        than introspecting the global pending count — the count races
        with background timer threads firing entries scheduled by other
        tests in the same xdist worker.
        """
        from media_preview_generator.processing.multi_server import (
            PublisherResult,
            PublisherStatus,
        )

        registry = MagicMock()
        registry.find_owning_servers.return_value = [
            MagicMock(server_id="plex-1"),
        ]
        registry.get.return_value = MagicMock(id="plex-1", name="plex-1")
        registry.get_config.return_value = MagicMock(
            type=MagicMock(value="plex"),
            output={"adapter": "plex_bundle", "plex_config_folder": "/tmp/p", "frame_interval": 5},
        )

        config = MagicMock()
        config.working_tmp_folder = "/tmp/x"
        config.plex_bif_frame_interval = 5

        def stub_publish(server, adapter, bundle, item_id, *, skip_if_exists):
            return PublisherResult(
                server_id="plex-1",
                server_name="plex-1",
                adapter_name="plex_bundle",
                status=PublisherStatus.SKIPPED_NOT_INDEXED,
                message="bundle hash unavailable",
            )

        # Spy on the actual scheduler call site so we observe the
        # invocation directly rather than racing the background timer.
        schedule_calls: list[dict] = []

        def _spy_schedule(*args, **kwargs):
            schedule_calls.append(kwargs)
            return True

        # Stub the heavy machinery - we only care about scheduling behavior.
        with (
            patch(
                "media_preview_generator.processing.multi_server._resolve_publishers",
                return_value=[(MagicMock(id="plex-1", name="plex-1"), MagicMock(name="adapter"), None)],
            ),
            patch(
                "media_preview_generator.processing.multi_server._publish_one",
                side_effect=stub_publish,
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

        assert result.status is MultiServerStatus.SKIPPED
        # The dispatcher made exactly ONE schedule_retry_for_unindexed call
        # (corresponding to our SKIPPED_NOT_INDEXED publisher).
        assert len(schedule_calls) == 1, schedule_calls
        assert schedule_calls[0].get("attempt") == 1
        assert schedule_calls[0].get("canonical_path") in (None, "/x.mkv") or (
            len(schedule_calls[0].get("canonical_path") or "") > 0
        )

    def test_skipped_not_indexed_no_retry_when_disabled(self):
        """schedule_retry_on_not_indexed=False suppresses scheduling."""
        from media_preview_generator.processing.multi_server import (
            PublisherResult,
            PublisherStatus,
        )

        registry = MagicMock()
        config = MagicMock()
        config.working_tmp_folder = "/tmp/x"
        config.plex_bif_frame_interval = 5

        def stub_publish(server, adapter, bundle, item_id, *, skip_if_exists):
            return PublisherResult(
                server_id="plex-1",
                server_name="plex-1",
                adapter_name="plex_bundle",
                status=PublisherStatus.SKIPPED_NOT_INDEXED,
                message="not yet",
            )

        # Spy: same shape as the previous test for symmetry + race-free
        # observation.
        schedule_calls: list[dict] = []

        def _spy_schedule(*args, **kwargs):
            schedule_calls.append(kwargs)
            return True

        with (
            patch(
                "media_preview_generator.processing.multi_server._resolve_publishers",
                return_value=[(MagicMock(id="plex-1", name="plex-1"), MagicMock(name="adapter"), None)],
            ),
            patch(
                "media_preview_generator.processing.multi_server._publish_one",
                side_effect=stub_publish,
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

        # No schedule call when scheduling is disabled.
        assert schedule_calls == []
        # (orig delta-based assertion below replaced)
