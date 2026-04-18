"""Unit tests for the retry-cascade classifier helpers.

Covers :mod:`plex_generate_previews.processing.retry_cascade` — the
pure predicate layer pulled out of ``generate_images`` so retry reason
classification is unit-testable without spinning up FFmpeg.
"""

import pytest

from plex_generate_previews.processing.retry_cascade import (
    RetryTier,
    classify_cpu_fallback_reason,
    classify_dv_safe_retry_reason,
)


class TestRetryTierEnum:
    def test_all_known_tiers_present(self):
        assert {t.value for t in RetryTier} == {
            "none",
            "skip_frame",
            "sw_libplacebo",
            "dv_safe_filter",
            "cpu_fallback",
        }


class TestClassifyDvSafeRetryReason:
    def test_no_stderr_returns_none(self):
        assert classify_dv_safe_retry_reason([], use_libplacebo=False) is None

    def test_dv_rpu_stderr_picks_dv_label(self):
        # Real upstream FFmpeg signature.
        stderr = [
            "Multiple Dolby Vision RPUs found in one AU. Skipping previous.",
        ]
        assert (
            classify_dv_safe_retry_reason(stderr, use_libplacebo=False)
            == "Dolby Vision RPU parsing error"
        )

    def test_zscale_stderr_picks_zscale_label(self):
        # Real zscale error shape from FFmpeg when tonemapping DV5.
        stderr = [
            "[Parsed_zscale_1 @ 0x55eb] no path between colorspaces",
        ]
        assert (
            classify_dv_safe_retry_reason(stderr, use_libplacebo=False)
            == "zscale colorspace conversion error"
        )

    def test_libplacebo_failure_only_fires_when_libplacebo_was_active(self):
        # Generic failure stderr that doesn't match DV-RPU or zscale
        # patterns; libplacebo was active so we should catch it here.
        stderr = ["Conversion failed"]
        assert (
            classify_dv_safe_retry_reason(stderr, use_libplacebo=True)
            == "libplacebo tone mapping error"
        )

    def test_libplacebo_failure_ignored_without_libplacebo(self):
        stderr = ["Conversion failed"]
        assert classify_dv_safe_retry_reason(stderr, use_libplacebo=False) is None

    def test_dv_rpu_wins_over_libplacebo_catchall(self):
        # Both conditions present; DV label should be preferred as it's
        # the more specific diagnosis.
        stderr = [
            "Multiple Dolby Vision RPUs found in one AU",
            "Conversion failed",
        ]
        assert (
            classify_dv_safe_retry_reason(stderr, use_libplacebo=True)
            == "Dolby Vision RPU parsing error"
        )


class TestClassifyCpuFallbackReason:
    @pytest.fixture
    def predicates(self):
        """Injectable detector stubs, overridable per-test."""
        return {
            "detect_codec_error": lambda rc, lines: False,
            "detect_hwaccel_runtime_error": lambda lines: False,
            "is_signal_killed": lambda rc: False,
        }

    def test_no_signal_no_codec_no_hwaccel_returns_false(self, predicates):
        should, reason = classify_cpu_fallback_reason(1, [], [], **predicates)
        assert should is False
        assert reason is None

    def test_codec_error_wins_first(self, predicates):
        predicates["detect_codec_error"] = lambda rc, lines: True
        predicates["detect_hwaccel_runtime_error"] = lambda lines: True
        predicates["is_signal_killed"] = lambda rc: True
        should, reason = classify_cpu_fallback_reason(
            234, ["hevc: no decoder"], ["..."], **predicates
        )
        assert should is True
        assert reason == "codec error"

    def test_hwaccel_runtime_error_wins_over_signal(self, predicates):
        predicates["detect_hwaccel_runtime_error"] = lambda lines: True
        predicates["is_signal_killed"] = lambda rc: True
        should, reason = classify_cpu_fallback_reason(
            139, [], ["Failed to sync surface"], **predicates
        )
        assert should is True
        assert reason == "hardware accelerator runtime error"

    def test_signal_kill_reports_signal_number(self, predicates):
        predicates["is_signal_killed"] = lambda rc: True
        # 139 = 128 + 11 (SIGSEGV)
        should, reason = classify_cpu_fallback_reason(139, [], [], **predicates)
        assert should is True
        assert reason == "signal kill (signal 11)"

    def test_low_signal_number_does_not_subtract_128(self, predicates):
        """rc <= 128 is not a signal-encoded exit but is still surfaced."""
        predicates["is_signal_killed"] = lambda rc: True
        should, reason = classify_cpu_fallback_reason(9, [], [], **predicates)
        assert should is True
        assert reason == "signal kill (signal 9)"
