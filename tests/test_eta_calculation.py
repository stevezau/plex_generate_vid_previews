"""
Tests for the ETA calculation algorithm in web/routes.py.

The ETA logic lives inside the progress_callback closure created by
_start_job_async.  These tests extract the same dual-track algorithm
into a helper and exercise it with simulated completion sequences.
"""

import time


# ---------------------------------------------------------------------------
# Minimal harness that mirrors the closure variables + progress_callback
# from _start_job_async in web/routes.py.
# ---------------------------------------------------------------------------


class _ETAHarness:
    """Standalone replica of the ETA closure for unit testing."""

    _SKIP_THRESHOLD = 2.0
    _STALL_THRESHOLD = 5.0
    _SIMPLE_MIN_ELAPSED = 20.0
    _SIMPLE_MIN_ITEMS = 2

    def __init__(self):
        self._last_total = 0
        self._processing_start_time = 0.0
        self._last_completed = 0
        self._last_completion_time = 0.0
        self._burst_resolved = False
        self._real_work_start_time = 0.0
        self._real_work_start_count = 0
        self.last_eta = ""
        self.last_percent = 0.0

    @staticmethod
    def _format_eta(seconds: float) -> str:
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            return f"{int(seconds // 60)}m {int(seconds % 60)}s"
        else:
            return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"

    def progress_callback(
        self, current: int, total: int, message: str = "", *, now: float = None
    ):
        """Simulate the progress_callback from routes.py.

        Accepts an explicit *now* timestamp so tests can drive time
        deterministically without sleeping.
        """
        if now is None:
            now = time.time()

        percent = (current / total * 100) if total > 0 else 0

        # Reset on library change
        if total != self._last_total:
            self._last_total = total
            self._last_completed = 0
            self._last_completion_time = 0.0
            self._processing_start_time = now
            self._burst_resolved = False
            self._real_work_start_time = 0.0
            self._real_work_start_count = 0

        new_items = current - self._last_completed
        if new_items > 0:
            self._last_completed = current
            self._last_completion_time = now

        remaining = total - current

        # Stall detection
        stall_time = 0.0
        if self._last_completion_time > 0 and remaining > 0:
            stall_time = now - self._last_completion_time

        # Track 1: burst-filtered
        if (
            not self._burst_resolved
            and self._processing_start_time > 0
            and current >= 2
        ):
            overall_elapsed = now - self._processing_start_time
            avg_per_item = overall_elapsed / current
            if avg_per_item >= self._SKIP_THRESHOLD:
                self._burst_resolved = True
                self._real_work_start_time = self._processing_start_time
                self._real_work_start_count = 0

        # Stall-based burst resolution
        if not self._burst_resolved and stall_time >= self._STALL_THRESHOLD:
            self._burst_resolved = True
            self._real_work_start_time = self._last_completion_time
            self._real_work_start_count = self._last_completed

        # Compute ETA
        eta = ""
        if remaining > 0:
            if self._burst_resolved and self._real_work_start_time > 0:
                real_elapsed = now - self._real_work_start_time
                real_items = current - self._real_work_start_count
                if real_elapsed > 0 and real_items >= 1:
                    rate = real_items / real_elapsed
                    eta = self._format_eta(remaining / rate)

            if (
                not eta
                and self._processing_start_time > 0
                and current >= self._SIMPLE_MIN_ITEMS
                and stall_time < self._STALL_THRESHOLD
            ):
                elapsed = now - self._processing_start_time
                if elapsed >= self._SIMPLE_MIN_ELAPSED:
                    rate = current / elapsed
                    if rate > 0:
                        eta = self._format_eta(remaining / rate)

        self.last_eta = eta
        self.last_percent = percent
        return eta


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFormatEta:
    """Test the _format_eta helper."""

    def test_seconds_only(self):
        assert _ETAHarness._format_eta(45) == "45s"

    def test_minutes_and_seconds(self):
        assert _ETAHarness._format_eta(125) == "2m 5s"

    def test_hours_and_minutes(self):
        assert _ETAHarness._format_eta(3700) == "1h 1m"

    def test_zero(self):
        assert _ETAHarness._format_eta(0) == "0s"


class TestBurstDetection:
    """Test that burst mode detects fast-skip vs real-work items."""

    def test_pure_burst_stays_in_burst(self):
        """100 items completing in 1 second each → avg < 2 s → burst not resolved."""
        h = _ETAHarness()
        t0 = 1_000_000.0
        # Simulate 100 items completing 1 per second
        for i in range(1, 101):
            h.progress_callback(i, 200, now=t0 + i)
        assert not h._burst_resolved
        # Fallback ETA kicks in (100 items, ~100 s elapsed > 20 s warmup)
        assert h.last_eta != "", "Fallback ETA should be shown even during burst"

    def test_slow_items_exit_burst(self):
        """Items averaging ≥2 s each → burst resolved."""
        h = _ETAHarness()
        t0 = 1_000_000.0
        # 4 items over 20 seconds → 5 s/item on average
        for i in range(1, 5):
            h.progress_callback(i, 100, now=t0 + i * 5)
        assert h._burst_resolved

    def test_eta_shown_after_burst_resolved(self):
        """Once burst resolves, ETA is computed immediately."""
        h = _ETAHarness()
        t0 = 1_000_000.0
        total = 100
        # 2 items at 5 s each → avg = 5 s → resolved
        h.progress_callback(1, total, now=t0 + 5)
        h.progress_callback(2, total, now=t0 + 10)
        assert h._burst_resolved
        eta = h.last_eta
        assert eta != "", "ETA should be shown once burst resolves"


class TestParallelWorkerBatching:
    """Verify ETA works when items complete in batches (multi-worker)."""

    def test_batch_completion_resolves_burst(self):
        """4 workers, 3-minute items.  Batch 1 at T=180, items 1-4.
        avg = 180/4 = 45 s/item → burst resolved at first batch."""
        h = _ETAHarness()
        t0 = 1_000_000.0
        total = 100
        # Processing starts at T=0 (initial callback sets _processing_start_time)
        h.progress_callback(0, total, now=t0)
        # Batch 1: items 1-4 complete at T=180 (within same poll cycle)
        for i in range(1, 5):
            h.progress_callback(i, total, now=t0 + 180 + i * 0.001)
        assert h._burst_resolved, "Burst should resolve with avg 45 s/item"
        assert h.last_eta != "", "ETA should appear after batch 1"

    def test_poll_between_batches_updates_eta(self):
        """on_poll calls between batches should keep updating ETA."""
        h = _ETAHarness()
        t0 = 1_000_000.0
        total = 100

        # Processing starts at T=0
        h.progress_callback(0, total, now=t0)

        # Batch 1 at T=180
        for i in range(1, 5):
            h.progress_callback(i, total, now=t0 + 180 + i * 0.001)

        eta1 = h.last_eta
        assert eta1 != ""

        # Simulate poll 30 seconds later (no new items)
        h.progress_callback(4, total, now=t0 + 210)
        eta2 = h.last_eta
        assert eta2 != "", "ETA should still be shown on poll"


class TestSimpleFallback:
    """Test the simple-rate fallback for when burst detection doesn't fire."""

    def test_fallback_after_warmup(self):
        """All items fast (burst), but after 20 s + 2 items → fallback ETA.

        The simple-rate fallback only fires when NOT stalling (stall_time
        < _STALL_THRESHOLD).  So the poll must happen soon after a
        completion (within 5 s) to pass the stall guard.
        """
        h = _ETAHarness()
        t0 = 1_000_000.0
        total = 1000
        # 50 items in 0.5 s each → avg = 0.5 s → burst NOT resolved
        # Total elapsed = 25 s → meets _SIMPLE_MIN_ELAPSED (20 s)
        for i in range(1, 51):
            h.progress_callback(i, total, now=t0 + i * 0.5)
        assert not h._burst_resolved
        # Last completion at t0 + 25, poll immediately → stall < 5 s
        assert h.last_eta != "", "Fallback ETA should kick in after 20 s elapsed"

    def test_fallback_not_premature(self):
        """Fallback should NOT fire before _SIMPLE_MIN_ELAPSED."""
        h = _ETAHarness()
        t0 = 1_000_000.0
        total = 100
        # 5 items in 2 s each = 10 s total → avg 2 s → burst resolved
        # But let's test the fallback path: force burst unresolved
        for i in range(1, 4):
            h.progress_callback(i, total, now=t0 + i * 0.5)
        assert not h._burst_resolved  # avg = 0.5 s < 2
        # Only 1.5 s elapsed, 3 items → SIMPLE_MIN_ELAPSED not met
        assert h.last_eta == ""


class TestLibraryReset:
    """Test that state resets when a new library starts (total changes)."""

    def test_reset_on_total_change(self):
        """Switching libraries resets all ETA state."""
        h = _ETAHarness()
        t0 = 1_000_000.0

        # Library 1: 50 items, get ETA working
        for i in range(1, 11):
            h.progress_callback(i, 50, now=t0 + i * 5)
        assert h._burst_resolved
        assert h.last_eta != ""

        # Library 2 starts (different total)
        h.progress_callback(0, 30, now=t0 + 100)
        assert not h._burst_resolved, "State should reset for new library"
        assert h._last_completion_time == 0.0, "Completion time should reset"
        assert h.last_eta == "", "ETA should reset to empty"

    def test_eta_recovers_after_reset(self):
        """After reset, ETA reappears once enough data accumulates."""
        h = _ETAHarness()
        t0 = 1_000_000.0

        # Library 1
        for i in range(1, 6):
            h.progress_callback(i, 50, now=t0 + i * 5)

        # Library 2 starts
        h.progress_callback(0, 30, now=t0 + 50)
        # Process a few items in library 2
        for i in range(1, 5):
            h.progress_callback(i, 30, now=t0 + 50 + i * 10)
        assert h._burst_resolved
        assert h.last_eta != ""


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_zero_total(self):
        """total=0 should not crash."""
        h = _ETAHarness()
        eta = h.progress_callback(0, 0, now=1_000_000.0)
        assert eta == ""

    def test_current_equals_total(self):
        """When job is done, remaining=0, no ETA needed."""
        h = _ETAHarness()
        t0 = 1_000_000.0
        for i in range(1, 11):
            h.progress_callback(i, 10, now=t0 + i * 5)
        assert h.last_eta == "", "No ETA when remaining=0"

    def test_single_item(self):
        """Only 1 item in library — ETA should not crash."""
        h = _ETAHarness()
        h.progress_callback(0, 1, now=1_000_000.0)
        h.progress_callback(1, 1, now=1_000_005.0)
        assert h.last_eta == ""  # remaining=0

    def test_mixed_burst_then_slow(self):
        """Burst of fast items, then slow items → ETA appears."""
        h = _ETAHarness()
        t0 = 1_000_000.0
        total = 500
        # 100 fast items in 2 seconds total
        for i in range(1, 101):
            h.progress_callback(i, total, now=t0 + i * 0.02)
        assert not h._burst_resolved  # avg 0.02 s < 2 s

        # Now 10 slow items (3 min each, but wall-clock accumulates)
        # After 100 fast + 10 slow: avg = (2 + 1800) / 110 = 16.4 s → resolved
        for j in range(1, 11):
            h.progress_callback(100 + j, total, now=t0 + 2 + j * 180)
        assert h._burst_resolved
        assert h.last_eta != ""


class TestStallDetection:
    """Test stall-based burst resolution for the skip-then-process pattern."""

    def test_stall_resolves_burst(self):
        """1103 items skipped fast, then 5 s stall → burst resolved via stall."""
        h = _ETAHarness()
        t0 = 1_000_000.0
        total = 1107
        # Skip 1103 items in 30 seconds
        for i in range(1, 1104):
            h.progress_callback(i, total, now=t0 + i * (30.0 / 1103))
        assert not h._burst_resolved, "avg still < 2 s"

        # Stall: poll at +6 s with no new completions
        h.progress_callback(1103, total, now=t0 + 30 + 6)
        assert h._burst_resolved, "Stall should resolve burst"
        assert h._real_work_start_count == 1103

    def test_stall_suppresses_misleading_simple_eta(self):
        """During stall the simple-rate fallback must be suppressed."""
        h = _ETAHarness()
        t0 = 1_000_000.0
        total = 1107
        # Skip 1103 items in 30 seconds (well past 20 s warmup)
        for i in range(1, 1104):
            h.progress_callback(i, total, now=t0 + i * (30.0 / 1103))

        # Before stall threshold: simple fallback gives near-zero ETA
        h.progress_callback(1103, total, now=t0 + 30 + 3)
        # After stall threshold: ETA should be empty (Calculating...)
        h.progress_callback(1103, total, now=t0 + 30 + 6)
        assert h.last_eta == "", (
            "No ETA should show during stall until a real item completes"
        )

    def test_eta_recovers_after_real_item_completes(self):
        """Once a real item completes after stall, ETA is based on real rate."""
        h = _ETAHarness()
        t0 = 1_000_000.0
        total = 1107
        # Skip 1103 items in 30 seconds
        for i in range(1, 1104):
            h.progress_callback(i, total, now=t0 + i * (30.0 / 1103))

        # Stall for 6 s → burst resolves, real_work_start_count = 1103
        h.progress_callback(1103, total, now=t0 + 36)
        assert h._burst_resolved

        # Item 1104 completes after 180 s of real work
        h.progress_callback(1104, total, now=t0 + 30 + 180)
        assert h.last_eta != "", "ETA should appear after real item completes"
        # rate = 1 item / 180 s, remaining = 3, ETA ≈ 540 s = 9m 0s
        assert "m" in h.last_eta, f"ETA should be in minutes, got {h.last_eta}"

    def test_stall_does_not_trigger_during_normal_processing(self):
        """Items completing every 3 s should NOT trigger stall detection."""
        h = _ETAHarness()
        t0 = 1_000_000.0
        total = 100
        # Items every 3 seconds (avg = 3 s > 2 s → burst resolves via avg)
        for i in range(1, 21):
            h.progress_callback(i, total, now=t0 + i * 3)
        assert h._burst_resolved, "Should resolve via avg_per_item, not stall"
        assert h.last_eta != ""
