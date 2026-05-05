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

    def test_schedule_default_attempt_is_first_retry(self):
        """Mutation-testing closer (retry_queue.py:73 default `attempt: int = 1`).

        Every existing test passes ``attempt=`` explicitly; the default value
        is unobserved. Mutating ``attempt: int = 1`` to ``attempt: int = 0``
        survives because no test calls ``schedule()`` without the kwarg.
        With ``attempt=0``, the guard at L82 returns False and a bare-call
        site is silently broken.

        Pin: ``schedule()`` without an explicit attempt accepts the call
        (returns True, signalling the first retry was queued).
        """
        sched = RetryScheduler()
        with patch(
            "media_preview_generator.processing.retry_queue._BACKOFF",
            tuple([0.001] * 5),
        ):
            result = sched.schedule("/x.mkv", lambda *a, **kw: None)  # no attempt= kwarg
        assert result is True, (
            "Default `attempt: int = 1` must accept the call (1 is the first retry index). "
            "A regression that defaulted to 0 would silently fail every bare-call site."
        )
        # Cancel to avoid leaking the timer into other tests.
        sched.cancel("/x.mkv")

    def test_schedule_rejects_attempt_zero(self):
        """Mutation-testing closer (retry_queue.py:82 — `attempt < 1` boundary).

        ``attempt=0`` is invalid (the *upcoming* attempt index is 1-based).
        Production guards with ``if attempt < 1 or attempt > len(_BACKOFF)``
        — without this test, mutating ``attempt < 1`` to ``attempt < 0``
        survives because ``0 < 0`` is False, the guard misses, then
        ``_BACKOFF[0 - 1]`` wraps to ``_BACKOFF[-1] = 3600`` (1 hour) and
        the caller silently gets a 1-hour delayed retry instead of a
        clean give-up.

        Pin: ``schedule(..., attempt=0)`` returns False AND no timer was
        installed (pending_count stays 0).
        """
        sched = RetryScheduler()
        # Use a no-op cb that would record if it ever fired (it must not).
        fired: list = []
        result = sched.schedule("/x.mkv", lambda *a, **kw: fired.append(a), attempt=0)

        assert result is False, (
            "schedule(attempt=0) must return False (caller's give-up signal). "
            "A regression that flipped the guard to `attempt < 0` would silently "
            "schedule a retry in _BACKOFF[-1] = 3600s."
        )
        assert sched.pending_count() == 0, (
            "schedule(attempt=0) must NOT install a timer; _BACKOFF[-1] is a 1-hour silent retry under the mutation."
        )
        # And critically — no timer fired (defensive, since the real concern
        # is a mutated guard installing a 3600s timer).
        assert fired == [], "callback should never have been invoked for attempt=0"


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
        # No pin specified at schedule time → no pin at retry time.
        # (See test_callback_forwards_explicit_server_id_filter for the
        # pinned variant — final-audit MED regression pin.)
        assert captured[0]["server_id_filter"] is None

    @pytest.mark.parametrize(
        "originator_id",
        ["plex-default", "emby-test", "jelly-test"],
        ids=["plex-pin", "emby-pin", "jelly-pin"],
    )
    def test_callback_forwards_explicit_server_id_filter(self, originator_id):
        """Final-audit MED regression pin — retry callback MUST forward
        the explicit ``server_id_filter`` it was scheduled with, NOT
        sniff the pin off ``config.server_id_filter``.

        The dispatch pin can come from two sources:
        1. ``config.server_id_filter`` — vendor-webhook job-config pin.
        2. Originator-derived (worker.py per_item_pin = item.server_id) —
           lives ONLY on the function-param ``server_id_filter`` of
           ``process_canonical_path``, NOT on ``config``.

        Pre-fix, the retry callback read ``config.server_id_filter``
        only — case 2 silently lost the pin and the retry fanned out
        to every owning server. Bug-blind test gap that this row
        closes: kwargs were checked but ``server_id_filter`` wasn't.

        Parametrised across Plex / Emby / Jellyfin originators per
        ``.claude/rules/testing.md`` "cover the matrix" guidance.
        """
        registry = MagicMock(name="registry")
        # Config pin DELIBERATELY not set — proves the explicit param,
        # not config.server_id_filter, is what reaches the SUT.
        config = MagicMock(name="config")
        config.server_id_filter = None
        captured: list[dict] = []
        ran = threading.Event()

        def fake_process(**kwargs):
            captured.append(kwargs)
            ran.set()
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
                item_id_by_server={originator_id: "abc"},
                attempt=1,
                server_id_filter=originator_id,
            )
            assert ok is True
            assert ran.wait(timeout=2)

        assert len(captured) == 1
        assert captured[0]["server_id_filter"] == originator_id, (
            f"Retry MUST forward the explicit pin. "
            f"Expected server_id_filter={originator_id!r}, "
            f"got {captured[0]['server_id_filter']!r}. "
            f"Without this the originator-pinned dispatch's retry fans "
            f"out to non-originator servers — M4 contract violation."
        )

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

    @pytest.mark.parametrize(
        "pin",
        [None, "plex-1", "emby-1", "jelly-1"],
        ids=["no-pin", "plex-pin", "emby-pin", "jelly-pin"],
    )
    def test_dispatcher_forwards_server_id_filter_to_retry_schedule(self, _isolated_frame_cache, pin):  # noqa: PLR0913
        # Server id matches the pin so the publisher actually runs and
        # hits SKIPPED_NOT_INDEXED — without this, a non-matching id
        # would short-circuit at the pin filter (NO_OWNERS) and no
        # retry would be scheduled, masking the assertion we want.
        # The ``no-pin`` case keeps a stable id; pin filtering is a
        # no-op when ``pin is None``.
        server_id_for_run = pin or "plex-1"
        """Final-audit MED regression pin — the dispatcher's call to
        ``schedule_retry_for_unindexed`` MUST forward the same
        ``server_id_filter`` it ran with, so the retry inherits the
        same dispatch pin instead of fanning out.

        Pre-fix the dispatcher passed only ``config`` and the retry
        queue read ``config.server_id_filter``. That path missed the
        worker's originator-derived pin (worker.py case 2) which lives
        on the function-param ``server_id_filter``, NOT on
        ``config.server_id_filter``.

        Parametrised across ``no-pin`` (peer-equal fanout retry) and
        each vendor pin (M4 contract: pinned dispatch retries to the
        same server only) per ``.claude/rules/testing.md`` "cover the
        matrix" guidance.
        """
        registry = MagicMock()
        server = MagicMock(id=server_id_for_run, name=server_id_for_run)
        adapter = self._make_adapter(raise_not_indexed=True)

        config = MagicMock()
        config.working_tmp_folder = "/tmp/x"
        config.plex_bif_frame_interval = 5
        config.thumbnail_interval = 5
        # Keep config.server_id_filter unset to prove the dispatcher
        # forwards the FUNCTION-PARAMETER ``server_id_filter``, not the
        # config attribute. (config_attr-only would mask the originator-
        # pin case.)
        config.server_id_filter = None

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
                server_id_filter=pin,
            )

        # The dispatcher MUST have scheduled exactly one retry with the
        # same pin it ran with — proves the function-parameter
        # ``server_id_filter`` is plumbed through, not just the config
        # attribute.
        assert len(schedule_calls) == 1, schedule_calls
        kw = schedule_calls[0]["kwargs"]
        assert kw.get("server_id_filter") == pin, (
            f"Dispatcher MUST forward the function-parameter pin to retry. "
            f"Expected server_id_filter={pin!r}, got {kw.get('server_id_filter')!r}. "
            f"Pre-fix the dispatcher silently dropped non-config pins, breaking "
            f"the M4 contract for originator-pinned webhooks (worker.py case 2)."
        )

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

            result = process_canonical_path(
                canonical_path="/x.mkv",
                registry=registry,
                config=config,
                use_frame_cache=False,
                schedule_retry_on_not_indexed=False,
            )

        # Audit fix — original asserted only ``schedule_calls == []``.
        # A regression where the disabled flag silently ALSO short-circuited
        # publishing entirely (skipping the not-indexed branch) would have
        # passed. Now also assert the not-indexed branch DID run by checking
        # the dispatcher's status / publisher accounting.
        assert schedule_calls == [], "schedule_retry_for_unindexed must NOT fire when disabled"
        # The not-indexed branch should have run and produced a SKIPPED-
        # variant status (it's the disabled retry-scheduler we're testing,
        # not disabled publishing). The exact variant depends on the
        # adapter's signal — accept any SKIPPED_* or the bare SKIPPED.
        status_name = result.status.name
        assert status_name == "SKIPPED" or status_name.startswith("SKIPPED_"), (
            f"the disabled-retry path should still attempt publishing and report SKIPPED_*; "
            f"got {result.status} — the disabled flag silently short-circuited the whole publish loop"
        )
