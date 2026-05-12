"""Unit tests for the WebhookDebouncer class.

Pin the contracts the class is responsible for; the integration with
``webhooks.py`` is exercised separately by the wider webhook test
suite once Step 3 of the refactor wires the class in.
"""

from __future__ import annotations

import threading
import time

import pytest

from media_preview_generator.web.webhook_debouncer import RECENT_DISPATCH_TTL_SECONDS, WebhookDebouncer


class TestInitialState:
    def test_new_instance_has_empty_state(self):
        d = WebhookDebouncer()
        assert d.pending_timers == {}
        assert d.pending_batches == {}
        assert d.recent_dispatches == {}

    def test_each_instance_is_isolated(self):
        """The whole point of the refactor: per-instance state.

        Two instances must NOT share state — that was the module-global
        bug that caused 19 separate ``_reset_singletons`` fixtures
        across the test suite. If this test ever fails, the class has
        regressed to module-level state and the refactor is undone.
        """
        d1 = WebhookDebouncer()
        d2 = WebhookDebouncer()
        d1.pending_batches["sonarr"] = {"file_paths": {"/x.mkv"}}
        assert "sonarr" not in d2.pending_batches


class TestContextManager:
    def test_with_block_provides_lock_atomicity(self):
        """``with debouncer:`` must hold the lock so compound operations
        (used by ``_schedule_webhook_job``) are atomic against a
        concurrent Timer-callback's ``pop_for_fire``.
        """
        d = WebhookDebouncer()
        # Holding the lock should block another thread's high-level
        # call (which also acquires the lock) until exit.
        other_acquired = threading.Event()

        def other_thread():
            d.check_and_record_dedup("sonarr", None, "/x.mkv", time.time())
            other_acquired.set()

        with d:
            t = threading.Thread(target=other_thread, daemon=True)
            t.start()
            # other thread should be BLOCKED on the lock — give it a
            # generous slice to prove it's not racing through.
            assert not other_acquired.wait(timeout=0.1), (
                "Context manager didn't hold the lock — other thread completed "
                "check_and_record_dedup while we were still inside `with debouncer:`"
            )
        # After exit, the other thread completes.
        assert other_acquired.wait(timeout=2.0)


class TestDedup:
    def test_first_dispatch_returns_none(self):
        d = WebhookDebouncer()
        now = time.time()
        assert d.check_and_record_dedup("sonarr", "s1", "/show/ep.mkv", now) is None

    def test_duplicate_within_ttl_returns_age(self):
        d = WebhookDebouncer()
        now = time.time()
        d.check_and_record_dedup("sonarr", "s1", "/show/ep.mkv", now)
        # 30s later, same key — should report ~30s.
        age = d.check_and_record_dedup("sonarr", "s1", "/show/ep.mkv", now + 30)
        assert age == 30

    def test_dispatch_beyond_ttl_is_not_dedup(self):
        d = WebhookDebouncer()
        now = time.time()
        d.check_and_record_dedup("sonarr", "s1", "/show/ep.mkv", now)
        # After the TTL window, the entry is expired and the new
        # dispatch is processed (not deduped). Pins the eviction
        # branch — without it, a long-running install would accumulate
        # the table without bound.
        beyond = now + RECENT_DISPATCH_TTL_SECONDS + 1
        assert d.check_and_record_dedup("sonarr", "s1", "/show/ep.mkv", beyond) is None

    def test_different_sources_for_same_path_are_not_dedup(self):
        """``(source, server_id, path)`` is the dedup key — same path
        from a different source is NOT a duplicate. This pins the
        cross-source semantics noted in the original dedup helper
        docstring: Plex's ``library.new`` + Sonarr's import for the
        same file are intentionally separate jobs.
        """
        d = WebhookDebouncer()
        now = time.time()
        d.check_and_record_dedup("sonarr", "s1", "/show/ep.mkv", now)
        # Same path, different source → fresh dispatch (no age).
        assert d.check_and_record_dedup("plex", "s1", "/show/ep.mkv", now) is None

    def test_different_server_ids_for_same_path_are_not_dedup(self):
        """Multi-server pin: same source + same path but a different
        server_id is NOT a duplicate. Plex-default and Plex-secondary
        receiving the same library.new for a shared mount point are
        intentionally separate dispatches. A regression that dropped
        ``server_id`` from the dedup key would not be caught without
        this row.
        """
        d = WebhookDebouncer()
        now = time.time()
        d.check_and_record_dedup("plex", "plex-default", "/movies/foo.mkv", now)
        assert d.check_and_record_dedup("plex", "plex-secondary", "/movies/foo.mkv", now) is None

    def test_none_and_empty_server_id_collapse_to_same_key(self):
        """The key shape ``(source, server_id or "", path)`` explicitly
        collapses ``None`` and ``""``. Both represent "no server pin";
        a webhook that arrives with one and a duplicate that arrives
        with the other must dedup. Without this row, a future
        refactor that distinguished them (e.g. by switching to
        ``str(server_id)`` which would render None as ``"None"``)
        would silently break dedup for Sonarr/Radarr (which always
        send server_id=None on the no-pin path).
        """
        d = WebhookDebouncer()
        now = time.time()
        d.check_and_record_dedup("sonarr", None, "/show/ep.mkv", now)
        age = d.check_and_record_dedup("sonarr", "", "/show/ep.mkv", now + 5)
        assert age == 5


class TestFireNow:
    def test_returns_none_when_nothing_pending(self):
        d = WebhookDebouncer()
        assert d.fire_now("sonarr") is None

    def test_pops_batch_and_cancels_timer(self):
        """``fire_now`` must (a) remove the batch from
        ``pending_batches``, (b) remove + cancel the Timer in
        ``pending_timers``. Idempotency pin: a second call returns
        ``None``.

        The Timer is scheduled with a deliberately SHORT delay (50 ms)
        so that if ``fire_now`` regresses to NOT calling
        ``timer.cancel()``, the callback fires within the assertion
        window and ``timer_fired.wait(0.3)`` returns True. The
        previous 60s delay was bug-blind: the callback couldn't fire
        in 0.5s no matter what cancellation did.
        """
        d = WebhookDebouncer()
        timer_fired = threading.Event()

        def callback() -> None:
            timer_fired.set()

        with d:
            d.pending_batches["sonarr"] = {"source": "sonarr", "file_paths": {"/x.mkv"}}
            t = threading.Timer(0.05, callback)
            t.daemon = True
            d.pending_timers["sonarr"] = t
            t.start()

        popped = d.fire_now("sonarr")
        assert popped is not None
        assert popped["source"] == "sonarr"
        # Idempotency: second call returns None.
        assert d.fire_now("sonarr") is None
        # Underlying dicts cleared.
        assert "sonarr" not in d.pending_batches
        assert "sonarr" not in d.pending_timers
        # The Timer must NOT fire — proves cancellation actually
        # happened. 300ms window is 6x the 50ms delay; if cancel
        # didn't run, the event would be set well within the wait.
        assert not timer_fired.wait(timeout=0.3), (
            "Timer fired despite fire_now() being called — cancel() regression. "
            "fire_now is supposed to pop the dicts AND cancel the underlying Timer."
        )


class TestPopForFire:
    def test_returns_none_when_already_popped(self):
        """Pin the documented race-safety contract: ``pop_for_fire``
        returning ``None`` is a real production code path (operator
        presses ``/fire-now`` exactly as the Timer fires its own
        callback). A regression to ``KeyError`` on missing key would
        be caught by this test.
        """
        d = WebhookDebouncer()
        # No entry for "sonarr" — pop_for_fire must NOT raise.
        assert d.pop_for_fire("sonarr") is None

    def test_atomic_pop_no_cancel(self):
        """``pop_for_fire`` is called FROM the Timer's own callback —
        so it pops both the batch and the timer entry but does NOT
        cancel (the caller is the timer; cancelling itself is a no-op).
        """
        d = WebhookDebouncer()
        with d:
            d.pending_batches["sonarr"] = {"source": "sonarr"}
            # Use a Timer that won't fire (long delay) just as a
            # placeholder for the dict entry.
            t = threading.Timer(60, lambda: None)
            t.daemon = True
            d.pending_timers["sonarr"] = t

        popped = d.pop_for_fire("sonarr")
        assert popped == {"source": "sonarr"}
        assert "sonarr" not in d.pending_batches
        assert "sonarr" not in d.pending_timers
        t.cancel()  # housekeeping for the test — Timer would otherwise live until process exit


class TestCancelAll:
    def test_cancels_every_timer_and_clears_state(self):
        """The reset path used by ``/api/__test/reset`` (and graceful
        shutdown). Every Timer must be cancelled and all three dicts
        must end empty.
        """
        d = WebhookDebouncer()
        timers_fired: list[str] = []

        with d:
            for key in ("sonarr", "radarr", "plex"):
                d.pending_batches[key] = {"source": key}
                t = threading.Timer(60, lambda k=key: timers_fired.append(k))
                t.daemon = True
                d.pending_timers[key] = t
                t.start()
            d.recent_dispatches[("sonarr", "", "/x.mkv")] = time.time()

        d.cancel_all()

        assert d.pending_batches == {}
        assert d.pending_timers == {}
        assert d.recent_dispatches == {}
        # None of the cancelled Timers should fire.
        time.sleep(0.2)
        assert timers_fired == []


class TestPendingSnapshot:
    def test_empty_snapshot(self):
        d = WebhookDebouncer()
        assert d.pending_snapshot() == []

    def test_snapshot_includes_batch_metadata(self):
        from datetime import datetime, timezone

        d = WebhookDebouncer()
        fire_at = datetime.now(timezone.utc).timestamp() + 60
        with d:
            d.pending_batches["sonarr"] = {
                "source": "sonarr",
                "titles": ["Show S01E01"],
                "file_paths": {"/show/ep1.mkv", "/show/ep2.mkv"},
                "fire_at": fire_at,
            }
            t = threading.Timer(60, lambda: None)
            t.daemon = True
            d.pending_timers["sonarr"] = t

        snap = d.pending_snapshot()
        assert len(snap) == 1
        entry = snap[0]
        assert entry["key"] == "sonarr"
        assert entry["source"] == "sonarr"
        assert entry["file_count"] == 2
        assert entry["first_title"] == "Show S01E01"
        assert entry["fire_at"] is not None
        # remaining_seconds is approximately 60; allow slop for the
        # snapshot-taking wall time.
        assert 55 < entry["remaining_seconds"] <= 60
        t.cancel()  # test housekeeping


class TestGetWebhookDebouncerHelper:
    def test_raises_when_not_registered(self):
        """The accessor must fail loudly if create_app() didn't wire
        a debouncer onto the app. Silent ``None`` returns would let
        webhook handlers AttributeError much later in execution with
        no clear pointer back to the missing wiring.
        """
        from flask import Flask

        from media_preview_generator.web.webhook_debouncer import get_webhook_debouncer

        app = Flask(__name__)  # no debouncer registered
        with app.app_context(), pytest.raises(KeyError, match="webhook_debouncer"):
            get_webhook_debouncer()

    def test_returns_registered_instance(self):
        from flask import Flask

        from media_preview_generator.web.webhook_debouncer import get_webhook_debouncer

        app = Flask(__name__)
        debouncer = WebhookDebouncer()
        app.extensions["webhook_debouncer"] = debouncer
        with app.app_context():
            assert get_webhook_debouncer() is debouncer
