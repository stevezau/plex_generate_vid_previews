"""Tests for credits detection using FFmpeg blackdetect and silencedetect."""

from unittest.mock import MagicMock, patch

import pytest

from plex_generate_previews.credits_detection import (
    BlackFrame,
    CreditsDetectionConfig,
    SilenceRegion,
    _combine_detections,
    _run_blackdetect,
    _run_silencedetect,
    detect_credits,
)


# ---------------------------------------------------------------------------
# blackdetect parsing
# ---------------------------------------------------------------------------


class TestRunBlackdetect:
    def test_parses_black_frames(self):
        stderr_output = (
            "[blackdetect @ 0x55a] black_start:10.5 black_end:12.0 black_duration:1.5\n"
            "[blackdetect @ 0x55a] black_start:45.0 black_end:46.5 black_duration:1.5\n"
        )
        mock_proc = MagicMock()
        mock_proc.stderr = iter(stderr_output.splitlines(keepends=True))
        mock_proc.wait.return_value = 0

        with patch("plex_generate_previews.credits_detection.subprocess.Popen") as mock:
            mock.return_value = mock_proc
            frames = _run_blackdetect("video.mp4", seek_to=100.0)

        assert len(frames) == 2
        # Timestamps should be offset by seek_to
        assert frames[0].start == pytest.approx(110.5)
        assert frames[0].end == pytest.approx(112.0)
        assert frames[0].duration == pytest.approx(1.5)
        assert frames[1].start == pytest.approx(145.0)

    def test_returns_empty_on_no_output(self):
        mock_proc = MagicMock()
        mock_proc.stderr = iter(["frame=100 fps=30\n"])
        mock_proc.wait.return_value = 0

        with patch("plex_generate_previews.credits_detection.subprocess.Popen") as mock:
            mock.return_value = mock_proc
            frames = _run_blackdetect("video.mp4", seek_to=0)

        assert frames == []

    def test_cancellation_stops_processing(self):
        stderr_output = (
            "[blackdetect @ 0x55a] black_start:10.0 black_end:11.0 black_duration:1.0\n"
            "[blackdetect @ 0x55a] black_start:20.0 black_end:21.0 black_duration:1.0\n"
        )
        mock_proc = MagicMock()
        mock_proc.stderr = iter(stderr_output.splitlines(keepends=True))
        mock_proc.wait.return_value = -9

        cancel = MagicMock(side_effect=[False, True])

        with patch("plex_generate_previews.credits_detection.subprocess.Popen") as mock:
            mock.return_value = mock_proc
            frames = _run_blackdetect("video.mp4", seek_to=0, cancel_check=cancel)

        # Should have parsed first line, cancelled on second
        assert len(frames) <= 2
        mock_proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# silencedetect parsing
# ---------------------------------------------------------------------------


class TestRunSilencedetect:
    def test_parses_silence_regions(self):
        stderr_output = (
            "[silencedetect @ 0x55b] silence_start: 50.2\n"
            "[silencedetect @ 0x55b] silence_end: 53.7 | silence_duration: 3.5\n"
            "[silencedetect @ 0x55b] silence_start: 80.0\n"
            "[silencedetect @ 0x55b] silence_end: 85.0 | silence_duration: 5.0\n"
        )
        mock_proc = MagicMock()
        mock_proc.stderr = iter(stderr_output.splitlines(keepends=True))
        mock_proc.wait.return_value = 0

        with patch("plex_generate_previews.credits_detection.subprocess.Popen") as mock:
            mock.return_value = mock_proc
            regions = _run_silencedetect("video.mp4", seek_to=200.0)

        assert len(regions) == 2
        # Timestamps offset by seek_to
        assert regions[0].start == pytest.approx(250.2)
        assert regions[0].end == pytest.approx(253.7)
        assert regions[0].duration == pytest.approx(3.5)
        assert regions[1].start == pytest.approx(280.0)

    def test_returns_empty_on_no_silence(self):
        mock_proc = MagicMock()
        mock_proc.stderr = iter(["size=0kB time=01:00:00.00\n"])
        mock_proc.wait.return_value = 0

        with patch("plex_generate_previews.credits_detection.subprocess.Popen") as mock:
            mock.return_value = mock_proc
            regions = _run_silencedetect("video.mp4", seek_to=0)

        assert regions == []


# ---------------------------------------------------------------------------
# _combine_detections
# ---------------------------------------------------------------------------


class TestCombineDetections:
    def test_combined_black_and_silence(self):
        """Overlapping black+silence near end → high confidence."""
        black = [BlackFrame(start=1800.0, end=1801.0, duration=1.0)]
        silence = [SilenceRegion(start=1799.5, end=1802.0, duration=2.5)]

        result = _combine_detections(
            black, silence, total_duration_sec=2000.0, min_credits_duration_sec=15.0
        )
        assert result is not None
        assert result.method == "black+silence"
        assert result.confidence == 0.8  # single black + silence
        assert result.start_ms == int(1799.5 * 1000)
        assert result.end_ms == 2000000

    def test_cluster_black_and_silence(self):
        """Multiple black frames + silence → highest confidence (0.9)."""
        black = [
            BlackFrame(start=1800.0, end=1801.0, duration=1.0),
            BlackFrame(start=1810.0, end=1811.0, duration=1.0),
        ]
        silence = [SilenceRegion(start=1799.5, end=1802.0, duration=2.5)]

        result = _combine_detections(
            black, silence, total_duration_sec=2000.0, min_credits_duration_sec=15.0
        )
        assert result is not None
        assert result.method == "black+silence"
        assert result.confidence == 0.9

    def test_black_cluster_only_fallback(self):
        """Multiple black frames without silence → medium confidence."""
        black = [
            BlackFrame(start=1800.0, end=1801.0, duration=1.0),
            BlackFrame(start=1810.0, end=1811.0, duration=1.0),
        ]

        result = _combine_detections(
            black, [], total_duration_sec=2000.0, min_credits_duration_sec=15.0
        )
        assert result is not None
        assert result.method == "black_cluster"
        assert result.confidence == 0.6

    def test_single_black_only_rejected(self):
        """Single black frame without silence → no match (avoids scene transition false positive)."""
        black = [BlackFrame(start=1800.0, end=1801.0, duration=1.0)]

        result = _combine_detections(
            black, [], total_duration_sec=2000.0, min_credits_duration_sec=15.0
        )
        assert result is None  # single black frame rejected — need cluster or silence

    def test_silence_only_fallback(self):
        """Silence without black frames → lower confidence."""
        silence = [SilenceRegion(start=1800.0, end=1805.0, duration=5.0)]

        result = _combine_detections(
            [], silence, total_duration_sec=2000.0, min_credits_duration_sec=15.0
        )
        assert result is not None
        assert result.method == "silence_only"
        assert result.confidence == 0.4

    def test_rejects_credits_too_early(self):
        """Detections before 75% of the video are rejected."""
        black = [BlackFrame(start=500.0, end=501.0, duration=1.0)]
        silence = [SilenceRegion(start=500.0, end=505.0, duration=5.0)]

        result = _combine_detections(
            black, silence, total_duration_sec=2000.0, max_credits_start_pct=75.0
        )
        assert result is None

    def test_rejects_credits_too_short(self):
        """Credits segment shorter than minimum duration is rejected."""
        black = [BlackFrame(start=1995.0, end=1996.0, duration=1.0)]
        silence = [SilenceRegion(start=1995.0, end=1998.0, duration=3.0)]

        result = _combine_detections(
            black, silence, total_duration_sec=2000.0, min_credits_duration_sec=15.0
        )
        assert result is None

    def test_no_detections_returns_none(self):
        result = _combine_detections([], [], total_duration_sec=2000.0)
        assert result is None

    def test_picks_earliest_qualifying_region(self):
        """When multiple regions qualify, pick the earliest."""
        black = [
            BlackFrame(start=1700.0, end=1701.0, duration=1.0),
            BlackFrame(start=1900.0, end=1901.0, duration=1.0),
        ]
        silence = [
            SilenceRegion(start=1700.0, end=1705.0, duration=5.0),
            SilenceRegion(start=1900.0, end=1905.0, duration=5.0),
        ]

        result = _combine_detections(
            black,
            silence,
            total_duration_sec=2000.0,
            min_credits_duration_sec=15.0,
            max_credits_start_pct=75.0,
        )
        assert result is not None
        assert result.start_ms == int(1700.0 * 1000)

    def test_adjacent_regions_combine(self):
        """Black and silence regions within tolerance combine."""
        black = [BlackFrame(start=1800.0, end=1801.0, duration=1.0)]
        silence = [SilenceRegion(start=1803.0, end=1807.0, duration=4.0)]

        result = _combine_detections(
            black, silence, total_duration_sec=2000.0, min_credits_duration_sec=15.0
        )
        assert result is not None
        assert result.method == "black+silence"
        # Credits start at the earlier of the two
        assert result.start_ms == int(1800.0 * 1000)


# ---------------------------------------------------------------------------
# detect_credits (end-to-end with mocked subprocess)
# ---------------------------------------------------------------------------


class TestDetectCredits:
    def test_returns_none_for_short_video(self):
        result = detect_credits("video.mp4", total_duration_sec=10.0)
        assert result is None

    @patch("plex_generate_previews.credits_detection._run_silencedetect")
    @patch("plex_generate_previews.credits_detection._run_blackdetect")
    def test_end_to_end_detection(self, mock_black, mock_silence):
        mock_black.return_value = [
            BlackFrame(start=2700.0, end=2701.0, duration=1.0),
        ]
        mock_silence.return_value = [
            SilenceRegion(start=2699.0, end=2703.0, duration=4.0),
        ]

        result = detect_credits("video.mp4", total_duration_sec=3600.0)
        assert result is not None
        assert result.method == "black+silence"
        assert result.start_ms == int(2699.0 * 1000)

    @patch("plex_generate_previews.credits_detection._run_silencedetect")
    @patch("plex_generate_previews.credits_detection._run_blackdetect")
    def test_respects_cancellation(self, mock_black, mock_silence):
        mock_black.return_value = []

        result = detect_credits(
            "video.mp4",
            total_duration_sec=3600.0,
            cancel_check=lambda: True,
        )
        assert result is None

    @patch("plex_generate_previews.credits_detection._run_silencedetect")
    @patch("plex_generate_previews.credits_detection._run_blackdetect")
    def test_custom_config(self, mock_black, mock_silence):
        mock_black.return_value = []
        mock_silence.return_value = []

        config = CreditsDetectionConfig(
            scan_last_pct=10.0,
            min_credits_duration=30.0,
        )
        detect_credits("video.mp4", total_duration_sec=3600.0, config=config)

        # Verify seek_to was computed from custom scan_last_pct
        call_args = mock_black.call_args
        assert call_args[0][1] == pytest.approx(3240.0)  # 3600 * 0.90
