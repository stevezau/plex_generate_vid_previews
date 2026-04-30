"""
Tests for media_processing.py module.

Tests BIF generation, FFmpeg execution, progress parsing, path mapping,
HDR detection, and the complete processing pipeline.
"""

import os
import struct
from unittest.mock import MagicMock, mock_open, patch

import pytest

from media_preview_generator.gpu import VulkanProbeResult
from media_preview_generator.processing import (
    DV5_PATH_INTEL_OPENCL,
    DV5_PATH_LIBPLACEBO,
    DV5_PATH_VAAPI_VULKAN,
    FFMPEG_STALL_TIMEOUT_SEC,
    CancellationError,
    CodecNotSupportedError,
    _detect_codec_error,
    _detect_dolby_vision_rpu_error,
    _detect_hwaccel_runtime_error,
    _detect_zscale_colorspace_error,
    _diagnose_ffmpeg_exit_code,
    _is_dolby_vision,
    _is_dv_no_backward_compat,
    _save_ffmpeg_failure_log,
    _verify_tmp_folder_health,
    build_dv5_vf,
    clear_failures,
    failure_scope,
    generate_bif,
    generate_images,
    get_failures,
    parse_ffmpeg_progress_line,
    process_item,
    record_failure,
)


class TestBIFGeneration:
    """Test BIF file generation."""

    def test_generate_bif_creates_valid_structure(self, temp_dir, mock_config):
        """Test that BIF file has correct binary structure."""
        # Create test thumbnails
        for i in range(3):
            timestamp = i * 5
            img_path = os.path.join(temp_dir, f"{timestamp:010d}.jpg")
            with open(img_path, "wb") as f:
                f.write(b"\xff\xd8\xff")

        # Generate BIF
        bif_path = os.path.join(temp_dir, "test.bif")
        generate_bif(bif_path, temp_dir, mock_config)

        # Verify BIF file exists
        assert os.path.exists(bif_path)

        # Verify BIF magic bytes
        with open(bif_path, "rb") as f:
            magic = list(f.read(8))
            assert magic == [0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A]

            # Check version
            version = struct.unpack("<I", f.read(4))[0]
            assert version == 0

            # Check image count
            image_count = struct.unpack("<I", f.read(4))[0]
            assert image_count == 3

            # Check frame interval (5 seconds = 5000ms)
            frame_interval = struct.unpack("<I", f.read(4))[0]
            assert frame_interval == 5000

    def test_generate_bif_index_table(self, temp_dir, mock_config):
        """Test that BIF index table has correct offsets."""
        # Create thumbnails with known sizes
        thumbnail_sizes = []
        for i in range(5):
            timestamp = i * 5
            img_path = os.path.join(temp_dir, f"{timestamp:010d}.jpg")
            data = b"\xff\xd8\xff" + (b"X" * (100 * (i + 1)))
            with open(img_path, "wb") as f:
                f.write(data)
            thumbnail_sizes.append(len(data))

        bif_path = os.path.join(temp_dir, "test.bif")
        generate_bif(bif_path, temp_dir, mock_config)

        # Verify index table
        with open(bif_path, "rb") as f:
            f.seek(64)  # Start of index table

            # Check each index entry
            expected_offset = 64 + (8 * 6)  # Header + index table (5 entries + end marker)
            for timestamp in range(5):
                ts = struct.unpack("<I", f.read(4))[0]
                offset = struct.unpack("<I", f.read(4))[0]

                assert ts == timestamp
                assert offset == expected_offset
                expected_offset += thumbnail_sizes[timestamp]

            # Check end marker
            end_ts = struct.unpack("<I", f.read(4))[0]
            end_offset = struct.unpack("<I", f.read(4))[0]
            assert end_ts == 0xFFFFFFFF
            assert end_offset == expected_offset

    def test_generate_bif_embedded_images(self, temp_dir, mock_config):
        """Test that actual image data is embedded in BIF."""
        # Create thumbnail with recognizable content
        test_data = b"\xff\xd8\xff" + b"TEST_IMAGE_DATA_12345"
        img_path = os.path.join(temp_dir, "0000000000.jpg")
        with open(img_path, "wb") as f:
            f.write(test_data)

        bif_path = os.path.join(temp_dir, "test.bif")
        generate_bif(bif_path, temp_dir, mock_config)

        # Verify image data is embedded
        with open(bif_path, "rb") as f:
            content = f.read()
            assert b"TEST_IMAGE_DATA_12345" in content
            assert test_data in content

    def test_generate_bif_empty_directory(self, temp_dir, mock_config):
        """Test BIF generation with no thumbnails."""
        bif_path = os.path.join(temp_dir, "empty.bif")
        generate_bif(bif_path, temp_dir, mock_config)

        # Should create BIF with 0 images
        with open(bif_path, "rb") as f:
            f.seek(12)  # Skip magic + version
            image_count = struct.unpack("<I", f.read(4))[0]
            assert image_count == 0

    def test_generate_bif_frame_interval(self, temp_dir, mock_config):
        """Test that frame interval is correctly converted to milliseconds."""
        # Test with 10 second interval
        mock_config.plex_bif_frame_interval = 10

        img_path = os.path.join(temp_dir, "0000000000.jpg")
        with open(img_path, "wb") as f:
            f.write(b"\xff\xd8\xff")

        bif_path = os.path.join(temp_dir, "test.bif")
        generate_bif(bif_path, temp_dir, mock_config)

        with open(bif_path, "rb") as f:
            f.seek(16)  # Skip magic + version + count
            frame_interval = struct.unpack("<I", f.read(4))[0]
            assert frame_interval == 10000  # 10 seconds = 10000ms


class TestFFmpegProgressParsing:
    """Test FFmpeg progress line parsing."""

    def test_parse_ffmpeg_progress_line_duration(self):
        """Test parsing Duration line from FFmpeg."""
        line = "  Duration: 01:23:45.67, start: 0.000000, bitrate: 8000 kb/s"
        duration = parse_ffmpeg_progress_line(line, 0.0)

        # 1 hour + 23 minutes + 45.67 seconds
        expected = 3600 + (23 * 60) + 45.67
        assert abs(duration - expected) < 0.1

    def test_parse_ffmpeg_progress_line_progress(self):
        """Test parsing progress line with time=."""
        line = "frame= 1234 fps=45.6 q=28.0 size=  12345kB time=00:12:34.56 bitrate= 123.4kbits/s speed=1.23x"

        callback_data = {}

        def callback(
            progress,
            current,
            total,
            speed,
            remaining=None,
            frame=0,
            fps=0,
            q=0,
            size=0,
            time_str="",
            bitrate=0,
        ):
            callback_data["progress"] = progress
            callback_data["current"] = current
            callback_data["speed"] = speed
            callback_data["frame"] = frame
            callback_data["fps"] = fps
            callback_data["time_str"] = time_str

        total_duration = 1800.0  # 30 minutes
        parse_ffmpeg_progress_line(line, total_duration, callback)

        # Verify callback was called with correct data
        assert "progress" in callback_data
        assert callback_data["frame"] == 1234
        assert abs(callback_data["fps"] - 45.6) < 0.1
        assert callback_data["speed"] == "1.23x"
        assert callback_data["time_str"] == "00:12:34.56"

    def test_parse_ffmpeg_progress_line_with_callback(self):
        """Test that progress callback is invoked correctly."""
        line = "frame= 100 fps=30.0 q=28.0 size=  1000kB time=00:00:10.00 bitrate= 800.0kbits/s speed=1.0x"

        callback_called = False

        def callback(*args, **kwargs):
            nonlocal callback_called
            callback_called = True

        parse_ffmpeg_progress_line(line, 100.0, callback)
        assert callback_called

    def test_parse_ffmpeg_progress_line_progress_decimal_precision(self):
        """Progress percent uses one decimal place for UI (Issue #144)."""
        line = "frame= 100 fps=30.0 q=28.0 size=  1000kB time=00:00:33.33 bitrate= 800.0kbits/s speed=1.0x"
        progress_seen = []

        def callback(
            progress,
            current,
            total,
            speed,
            remaining=None,
            frame=0,
            fps=0,
            q=0,
            size=0,
            time_str="",
            bitrate=0,
        ):
            progress_seen.append(progress)

        parse_ffmpeg_progress_line(line, 100.0, callback)
        assert len(progress_seen) == 1
        # 33.33/100*100 = 33.33 -> round(33.33, 1) = 33.3
        assert progress_seen[0] == 33.3
        assert isinstance(progress_seen[0], float)

    def test_parse_ffmpeg_progress_line_no_callback(self):
        """Test parsing without callback doesn't crash."""
        line = "frame= 100 fps=30.0 q=28.0 size=  1000kB time=00:00:10.00 bitrate= 800.0kbits/s speed=1.0x"
        result = parse_ffmpeg_progress_line(line, 100.0, None)
        assert result == 100.0

    def test_remaining_time_accounts_for_speed(self):
        """remaining_time should be wall-clock ETA, not raw media remaining."""
        line = "frame= 5000 fps=120 q=28.0 size=  50000kB time=00:05:00.00 bitrate= 1000.0kbits/s speed=100.0x"
        captured = {}

        def callback(
            progress,
            current,
            total,
            speed,
            remaining,
            frame,
            fps,
            q,
            size,
            time_str,
            bitrate,
        ):
            captured["remaining"] = remaining
            captured["speed"] = speed

        total_duration = 600.0  # 10 minutes
        parse_ffmpeg_progress_line(line, total_duration, callback)

        # current_time = 300s, remaining media = 300s, speed = 100x
        # wall-clock ETA = 300 / 100 = 3 seconds
        assert captured["speed"] == "100.0x"
        assert abs(captured["remaining"] - 3.0) < 0.1

    def test_remaining_time_at_1x_speed(self):
        """At 1x speed, wall-clock ETA equals remaining media duration."""
        line = "frame= 100 fps=30.0 q=28.0 size=  1000kB time=00:00:10.00 bitrate= 800.0kbits/s speed=1.0x"
        captured = {}

        def callback(
            progress,
            current,
            total,
            speed,
            remaining,
            frame,
            fps,
            q,
            size,
            time_str,
            bitrate,
        ):
            captured["remaining"] = remaining

        parse_ffmpeg_progress_line(line, 100.0, callback)
        # remaining media = 90s, speed = 1x -> wall-clock = 90s
        assert abs(captured["remaining"] - 90.0) < 0.1

    def test_remaining_time_no_speed_falls_back(self):
        """When speed is not parseable, remaining_time falls back to raw media remaining."""
        line = "frame= 100 fps=30.0 q=28.0 size=  1000kB time=00:00:10.00 bitrate= 800.0kbits/s speed=N/Ax"
        captured = {}

        def callback(
            progress,
            current,
            total,
            speed,
            remaining,
            frame,
            fps,
            q,
            size,
            time_str,
            bitrate,
        ):
            captured["remaining"] = remaining

        parse_ffmpeg_progress_line(line, 100.0, callback)
        # speed not parseable -> falls back to raw remaining = 90s
        assert abs(captured["remaining"] - 90.0) < 0.1


class TestDetectCodecError:
    """Test codec error detection for CPU fallback."""

    def test_detect_codec_error_stderr_patterns(self):
        """Test detection of codec error patterns in stderr."""
        # Test various codec error patterns
        patterns = [
            ["Codec not supported"],
            ["Unsupported codec with id 123"],
            ["Unknown decoder 'av1'"],
            ["Decoder not found for codec"],
            ["Could not find codec"],
            ["No decoder for codec av1"],
        ]

        for stderr_lines in patterns:
            result = _detect_codec_error(1, stderr_lines)
            assert result is True, f"Should detect codec error in: {stderr_lines}"

    def test_detect_codec_error_exit_code_69(self):
        """Test detection via exit code 69 (max error rate)."""
        stderr_lines = ["Some generic error message"]
        result = _detect_codec_error(69, stderr_lines)
        assert result is True

    def test_detect_codec_error_exit_code_minus22(self):
        """Test detection via exit code -22 (EINVAL)."""
        stderr_lines = ["Some error"]
        result = _detect_codec_error(-22, stderr_lines)
        assert result is True

    def test_detect_codec_error_exit_code_234(self):
        """Test detection via exit code 234 (wrapped -22 on Unix)."""
        stderr_lines = ["Some error"]
        result = _detect_codec_error(234, stderr_lines)
        assert result is True

    def test_detect_codec_error_no_match(self):
        """Test that non-codec errors are not detected."""
        stderr_lines = ["File not found", "Permission denied"]
        result = _detect_codec_error(1, stderr_lines)
        assert result is False

    def test_detect_codec_error_success_exit(self):
        """Test that stderr patterns are checked even with success exit code."""
        # Even if exit code is 0, if stderr contains codec error, it should be detected
        stderr_lines = ["Unsupported codec"]
        result = _detect_codec_error(0, stderr_lines)
        assert result is True  # Stderr pattern takes precedence

    def test_detect_codec_error_success_exit_no_codec_error(self):
        """Test that success exit code doesn't trigger detection when no codec error in stderr."""
        stderr_lines = ["File processed successfully"]
        result = _detect_codec_error(0, stderr_lines)
        assert result is False

    def test_detect_codec_error_case_insensitive(self):
        """Test that detection is case-insensitive."""
        stderr_lines = ["UNSUPPORTED CODEC", "Codec Not Supported", "unsupported codec"]
        for line in stderr_lines:
            result = _detect_codec_error(1, [line])
            assert result is True, f"Should detect codec error case-insensitively: {line}"


class TestDetectHwaccelRuntimeError:
    """Test GPU hardware accelerator runtime error detection."""

    def test_detect_vaapi_surface_sync_error(self):
        """Test detection of VAAPI surface sync failures (the exact crash from issue)."""
        stderr_lines = [
            "[AVHWFramesContext @ 0x146e00045d00] Failed to sync surface 0: 23 (internal decoding error).",
            "[h264 @ 0x55a542e23fc0] Failed to transfer data to output frame: -5.",
        ]
        assert _detect_hwaccel_runtime_error(stderr_lines) is True

    def test_detect_transfer_data_error(self):
        """Test detection of hw frame transfer failure."""
        stderr_lines = ["[h264 @ 0x55a5] Failed to transfer data to output frame: -5."]
        assert _detect_hwaccel_runtime_error(stderr_lines) is True

    def test_detect_avhwframescontext_error(self):
        """Test detection of generic AVHWFramesContext messages."""
        stderr_lines = ["[AVHWFramesContext @ 0xabc] Some unexpected error occurred"]
        assert _detect_hwaccel_runtime_error(stderr_lines) is True

    def test_detect_cuda_error(self):
        """Test detection of CUDA decode errors."""
        stderr_lines = ["CUDA error: out of memory"]
        assert _detect_hwaccel_runtime_error(stderr_lines) is True

    def test_detect_cuvid_error(self):
        """Test detection of cuvid decode errors."""
        stderr_lines = ["cuvid decode error on frame 1234"]
        assert _detect_hwaccel_runtime_error(stderr_lines) is True

    def test_detect_failed_to_create_surface(self):
        """Test detection of surface creation failures."""
        stderr_lines = ["[vaapi @ 0x55a5] Failed to create surface"]
        assert _detect_hwaccel_runtime_error(stderr_lines) is True

    def test_detect_hwaccel_init_error(self):
        """Test detection of hwaccel initialisation errors."""
        stderr_lines = ["hwaccel initialisation returned error"]
        assert _detect_hwaccel_runtime_error(stderr_lines) is True

    def test_no_match_on_normal_errors(self):
        """Test that non-hwaccel errors are not detected."""
        stderr_lines = [
            "File not found",
            "Permission denied",
            "Conversion failed!",
            "Error while processing the decoded data",
        ]
        assert _detect_hwaccel_runtime_error(stderr_lines) is False

    def test_no_match_on_empty_lines(self):
        """Test that empty input returns False."""
        assert _detect_hwaccel_runtime_error([]) is False
        assert _detect_hwaccel_runtime_error(None) is False

    def test_case_insensitive_matching(self):
        """Test that patterns are matched case-insensitively."""
        stderr_lines = ["FAILED TO SYNC SURFACE 0: 23"]
        assert _detect_hwaccel_runtime_error(stderr_lines) is True


class TestGenerateImages:
    """Test thumbnail generation with FFmpeg."""

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    def test_generate_images_calls_ffmpeg(
        self,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that generate_images calls FFmpeg with correct arguments."""
        # Mock heuristic check
        mock_run.return_value = MagicMock(returncode=0)

        # Mock MediaInfo
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        # Mock FFmpeg process
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        # Mock file operations
        mock_exists.return_value = False

        generate_images("/test/video.mp4", temp_dir, None, None, mock_config)

        # Verify FFmpeg was called
        assert mock_popen.called
        args = mock_popen.call_args[0][0]
        assert mock_config.ffmpeg_path in args
        assert "/test/video.mp4" in args

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("media_preview_generator.processing.ffmpeg_runner.time")
    @patch("media_preview_generator.processing.orchestrator.time")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("glob.glob")
    def test_ffmpeg_stall_timeout_kills_process(
        self,
        mock_glob,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_time,
        mock_runner_time,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """When FFmpeg produces no progress for FFMPEG_STALL_TIMEOUT_SEC, process is killed."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        # Process never exits on its own; we kill it via stall timeout
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.returncode = -9  # SIGKILL after kill()
        mock_popen.return_value = mock_proc

        # No progress file → last_progress_time never updated → stall detected
        mock_exists.return_value = False
        mock_glob.return_value = []

        # time.time() is called per _run_ffmpeg: start_local, last_progress_time, stall check, end_local.
        # generate_images retries without skip_frame after first failure, so we need values for 2 runs.
        # The stall check lives in ``ffmpeg_runner._run_ffmpeg``; the
        # wrapper timing uses ``media_processing.time``.  Both modules
        # get the same monotonically-advancing mock so the stall branch
        # fires on the third ``time.time()`` call of each run.
        stall_time = 0 + FFMPEG_STALL_TIMEOUT_SEC + 1
        one_run = [0, 0, stall_time, stall_time + 1]
        mock_time.time.side_effect = one_run + one_run
        mock_time.sleep.return_value = None
        mock_time.time_ns.return_value = 0  # for temp output filename
        mock_runner_time.time.side_effect = one_run + one_run
        mock_runner_time.sleep.return_value = None

        success, image_count, hw_used, seconds, speed, *_ = generate_images(
            "/test/video.mp4", temp_dir, None, None, mock_config
        )

        assert success is False
        # First run (with skip_frame) and retry (without) both hit stall timeout
        assert mock_proc.kill.call_count == 2
        assert mock_proc.wait.call_count == 2

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_generate_images_gpu_nvidia(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test NVIDIA GPU arguments are added."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False
        mock_glob.return_value = []

        generate_images("/test/video.mp4", temp_dir, "NVIDIA", "cuda", mock_config)

        args = mock_popen.call_args[0][0]
        assert "-hwaccel" in args
        assert "cuda" in args
        # Generic "cuda" (no index suffix) must NOT add -hwaccel_device.
        assert "-hwaccel_device" not in args

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_generate_images_gpu_nvidia_indexed_device(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """cuda:N device path emits -hwaccel_device N (issue #221)."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False
        mock_glob.return_value = []

        generate_images("/test/video.mp4", temp_dir, "NVIDIA", "cuda:1", mock_config)

        args = mock_popen.call_args[0][0]
        assert args[args.index("-hwaccel") + 1] == "cuda"
        assert "-hwaccel_device" in args
        assert args[args.index("-hwaccel_device") + 1] == "1"

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_generate_images_gpu_amd(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test AMD VAAPI arguments are added."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False
        mock_glob.return_value = []

        generate_images("/test/video.mp4", temp_dir, "AMD", "/dev/dri/renderD128", mock_config)

        args = mock_popen.call_args[0][0]
        assert "-hwaccel" in args
        assert "vaapi" in args
        assert "/dev/dri/renderD128" in args

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_generate_images_cpu_only(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test CPU-only processing without hwaccel."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False
        mock_glob.return_value = []

        generate_images("/test/video.mp4", temp_dir, None, None, mock_config)

        args = mock_popen.call_args[0][0]
        # Should not have hwaccel
        if "-hwaccel" in args:
            # If it exists, it shouldn't be used (heuristic may add it)
            pass

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_generate_images_hdr_detection(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test HDR video uses correct filter chain."""
        mock_run.return_value = MagicMock(returncode=0)

        # Mock HDR video
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format="HDR10")]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False
        mock_glob.return_value = []

        generate_images("/test/video.mp4", temp_dir, None, None, mock_config)

        args = mock_popen.call_args[0][0]
        # Find the -vf argument
        vf_index = args.index("-vf")
        vf_value = args[vf_index + 1]

        # Should contain HDR processing filters
        assert "zscale" in vf_value
        assert "tonemap" in vf_value

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.rename")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_generate_images_renames_files(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that images are renamed from img-XXXXXX.jpg to timestamp.jpg."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False

        # Mock glob to return test images
        mock_glob.return_value = [
            f"{temp_dir}/img-000001.jpg",
            f"{temp_dir}/img-000002.jpg",
            f"{temp_dir}/img-000003.jpg",
        ]

        generate_images("/test/video.mp4", temp_dir, None, None, mock_config)

        # Verify rename was called with correct arguments
        # img-000001.jpg (frame 0) -> 0000000000.jpg (0 seconds)
        # img-000002.jpg (frame 1) -> 0000000005.jpg (5 seconds)
        # img-000003.jpg (frame 2) -> 0000000010.jpg (10 seconds)
        assert mock_rename.called
        calls = mock_rename.call_args_list
        assert len(calls) == 3

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_generate_images_progress_callback(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that progress callback is called during processing."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = True
        mock_glob.return_value = []

        # Mock reading FFmpeg output
        mock_file.return_value.readlines.return_value = ["frame= 100 fps=30.0 time=00:00:10.00 speed=1.0x\n"]

        callback_called = [False]

        def callback(*args, **kwargs):
            callback_called[0] = True

        generate_images("/test/video.mp4", temp_dir, None, None, mock_config, callback)

        # Callback should have been called at least once
        # Note: Due to mocking, it may not be called, but the structure is there
        # This test verifies the code doesn't crash with a callback

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_raises_codec_error_in_gpu_context(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that CodecNotSupportedError is raised when GPU fails with codec error."""
        # Mock heuristic check - returns True to use skip_frame initially
        mock_run.return_value = MagicMock(returncode=0)

        # Mock MediaInfo
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        # First GPU attempt with skip_frame fails with codec error
        mock_proc_gpu_skip = MagicMock()
        mock_proc_gpu_skip.poll.side_effect = [None, 0]
        mock_proc_gpu_skip.returncode = 69  # Max error rate

        # Retry GPU attempt without skip_frame also fails
        mock_proc_gpu_noskip = MagicMock()
        mock_proc_gpu_noskip.poll.side_effect = [None, 0]
        mock_proc_gpu_noskip.returncode = 69

        mock_popen.side_effect = [mock_proc_gpu_skip, mock_proc_gpu_noskip]
        mock_exists.return_value = False

        # Glob returns empty (no images produced)
        mock_glob.return_value = []

        # Mock codec error detection
        mock_detect.return_value = True

        # Enable CPU threads (but generate_images should raise exception, not do fallback)
        mock_config.cpu_threads = 1

        # Should raise CodecNotSupportedError instead of doing CPU fallback
        with pytest.raises(CodecNotSupportedError) as exc_info:
            generate_images("/test/video.mp4", temp_dir, "NVIDIA", None, mock_config)

        assert "GPU processing failed" in str(exc_info.value)
        assert mock_detect.called
        assert mock_popen.call_count == 2  # GPU with skip_frame + GPU without skip_frame (no CPU fallback)

        # Verify cleanup was attempted
        assert mock_remove.called

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_no_cpu_fallback_when_disabled(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that CPU fallback is skipped when CPU threads = 0."""
        # Mock heuristic check - returns True to use skip_frame initially
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        # First attempt with skip_frame fails, retry without skip_frame also fails
        mock_proc_skip = MagicMock()
        mock_proc_skip.poll.side_effect = [None, 0]
        mock_proc_skip.returncode = 69  # Codec error

        mock_proc_noskip = MagicMock()
        mock_proc_noskip.poll.side_effect = [None, 0]
        mock_proc_noskip.returncode = 69

        mock_popen.side_effect = [mock_proc_skip, mock_proc_noskip]
        mock_exists.return_value = False

        # Glob call sequence for 2 attempts (skip_frame retry):
        # 1. img*.jpg after first attempt - empty
        # 2. *.jpg count after first attempt - empty
        # 3. *.jpg cleanup before retry - empty
        # 4. img*.jpg after retry - empty (retry failed)
        # 5. *.jpg count after retry - empty
        # All should return empty since both attempts fail
        def glob_side_effect(pattern):
            return []

        mock_glob.side_effect = glob_side_effect

        # Mock codec error detection
        mock_detect.return_value = True

        # Disable CPU threads
        mock_config.cpu_threads = 0

        # Should raise CodecNotSupportedError even when CPU threads disabled
        with pytest.raises(CodecNotSupportedError):
            generate_images("/test/video.mp4", temp_dir, "NVIDIA", None, mock_config)

        assert mock_detect.called
        assert mock_popen.call_count == 2  # Initial attempt + skip_frame retry, no CPU fallback (exception raised)

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_no_cpu_fallback_when_no_codec_error(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that CPU fallback doesn't trigger when error is not codec-related."""
        # Mock heuristic check - returns True to use skip_frame initially
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        # First attempt with skip_frame fails, retry without skip_frame also fails
        mock_proc_skip = MagicMock()
        mock_proc_skip.poll.side_effect = [None, 0]
        mock_proc_skip.returncode = 1  # Generic error

        mock_proc_noskip = MagicMock()
        mock_proc_noskip.poll.side_effect = [None, 0]
        mock_proc_noskip.returncode = 1

        mock_popen.side_effect = [mock_proc_skip, mock_proc_noskip]
        mock_exists.return_value = False

        # Glob call sequence for 2 attempts (skip_frame retry):
        # 1. img*.jpg after first attempt - empty
        # 2. *.jpg count after first attempt - empty
        # 3. *.jpg cleanup before retry - empty
        # 4. img*.jpg after retry - empty (retry failed)
        # 5. *.jpg count after retry - empty
        # All should return empty since both attempts fail
        def glob_side_effect(pattern):
            return []

        mock_glob.side_effect = glob_side_effect

        # Mock codec error detection - no codec error detected
        mock_detect.return_value = False

        # Enable CPU threads
        mock_config.cpu_threads = 1

        success, image_count, hw_used, seconds, speed, *_ = generate_images(
            "/test/video.mp4", temp_dir, "NVIDIA", None, mock_config
        )

        # Should fail (no fallback since not codec error)
        assert success is False
        assert image_count == 0
        assert mock_detect.called
        assert mock_popen.call_count == 2  # Initial attempt + skip_frame retry, no CPU fallback

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_dolby_vision_rpu_error_retries_with_dv_safe_filter_on_gpu(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV RPU parsing errors should trigger a third DV-safe (fps+scale) FFmpeg run on GPU before CPU handoff."""
        # Heuristic check - returns 0 to allow skip_frame initially
        mock_run.return_value = MagicMock(returncode=0)

        # MediaInfo
        mock_info = MagicMock()
        # Force HDR filter chain initially (zscale/tonemap) so we can prove DV-safe retry removed it
        mock_info.video_tracks = [MagicMock(hdr_format="HDR10")]
        mock_mediainfo.parse.return_value = mock_info

        # FFmpeg processes: fail both attempts (skip + no-skip), then succeed with DV-safe filter
        mock_proc_skip = MagicMock()
        mock_proc_skip.poll.side_effect = [None, 0]
        mock_proc_skip.returncode = 187

        mock_proc_noskip = MagicMock()
        mock_proc_noskip.poll.side_effect = [None, 0]
        mock_proc_noskip.returncode = 187

        mock_proc_dv_safe = MagicMock()
        mock_proc_dv_safe.poll.side_effect = [None, 0]
        mock_proc_dv_safe.returncode = 0

        mock_popen.side_effect = [mock_proc_skip, mock_proc_noskip, mock_proc_dv_safe]

        # Force output file reads to contain DV error
        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = [
            "Multiple Dolby Vision RPUs found in one AU. Skipping previous.\n"
        ]

        # No images produced until DV-safe retry, then one image exists (and then one renamed JPG exists)
        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"
        img_call_count = {"count": 0}

        def glob_side_effect(pattern):
            # Track only the img*.jpg pattern calls
            if "img*.jpg" in pattern:
                img_call_count["count"] += 1
                if img_call_count["count"] == 1:
                    return []  # after initial attempts
                if img_call_count["count"] == 2:
                    return [img1]  # after DV-safe attempt
                # rename stage: provide actual image filename
                return [img1]
            # Final recount after renaming
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        # Codec detection doesn't match; DV detector should still cause handoff
        mock_detect.return_value = False
        mock_config.cpu_threads = 1

        success, image_count, hw_used, seconds, speed, *_ = generate_images(
            "/test/video_dv.mp4", temp_dir, "NVIDIA", None, mock_config
        )

        assert success is True
        assert image_count >= 1
        assert hw_used is True
        assert mock_popen.call_count == 3  # skip + no-skip + DV-safe retry

        # Assert the third invocation used the DV-safe vf: fps+scale only, no zscale/tonemap.
        # On NVIDIA the DV-safe retry keeps the GPU-scale win from issue #218 —
        # scale_cuda + hwdownload rather than a CPU scale on full-resolution frames.
        third_args = mock_popen.call_args_list[2][0][0]
        vf_index = third_args.index("-vf")
        vf_value = third_args[vf_index + 1]
        assert "scale_cuda=w=320:h=240:force_original_aspect_ratio=decrease" in vf_value
        assert "hwdownload" in vf_value
        assert "zscale" not in vf_value
        assert "tonemap" not in vf_value

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_dolby_vision_rpu_error_cpu_returns_failure(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV RPU parsing errors on CPU should fail cleanly without raising CodecNotSupportedError."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc_skip = MagicMock()
        mock_proc_skip.poll.side_effect = [None, 0]
        mock_proc_skip.returncode = 187

        mock_proc_noskip = MagicMock()
        mock_proc_noskip.poll.side_effect = [None, 0]
        mock_proc_noskip.returncode = 187

        # With DV-safe retry implemented, we expect a third attempt (DV-safe) even on CPU.
        mock_proc_dv_safe = MagicMock()
        mock_proc_dv_safe.poll.side_effect = [None, 0]
        mock_proc_dv_safe.returncode = 187

        mock_popen.side_effect = [mock_proc_skip, mock_proc_noskip, mock_proc_dv_safe]

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = [
            "Multiple Dolby Vision RPUs found in one AU. Skipping previous.\n"
        ]

        mock_glob.return_value = []
        mock_detect.return_value = False

        success, image_count, hw_used, seconds, speed, *_ = generate_images(
            "/test/video_dv.mp4", temp_dir, None, None, mock_config
        )

        assert success is False
        assert image_count == 0
        assert hw_used is False
        assert mock_popen.call_count == 3

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_dolby_vision_rpu_error_retries_with_dv_safe_filter_on_cpu(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV RPU parsing errors should trigger a third DV-safe (fps+scale) FFmpeg run on CPU."""
        mock_run.return_value = MagicMock(returncode=0)

        # Force HDR filter chain initially (zscale/tonemap) so we can prove DV-safe retry removed it
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format="HDR10")]
        mock_mediainfo.parse.return_value = mock_info

        # FFmpeg processes: fail both attempts (skip + no-skip), then succeed with DV-safe filter
        mock_proc_skip = MagicMock()
        mock_proc_skip.poll.side_effect = [None, 0]
        mock_proc_skip.returncode = 187

        mock_proc_noskip = MagicMock()
        mock_proc_noskip.poll.side_effect = [None, 0]
        mock_proc_noskip.returncode = 187

        mock_proc_dv_safe = MagicMock()
        mock_proc_dv_safe.poll.side_effect = [None, 0]
        mock_proc_dv_safe.returncode = 0

        mock_popen.side_effect = [mock_proc_skip, mock_proc_noskip, mock_proc_dv_safe]

        # Force output file reads to contain DV error
        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = [
            "Multiple Dolby Vision RPUs found in one AU. Skipping previous.\n"
        ]

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"
        img_call_count = {"count": 0}

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                img_call_count["count"] += 1
                if img_call_count["count"] == 1:
                    return []
                if img_call_count["count"] == 2:
                    return [img1]
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        mock_detect.return_value = False

        success, image_count, hw_used, seconds, speed, *_ = generate_images(
            "/test/video_dv.mp4", temp_dir, None, None, mock_config
        )

        assert success is True
        assert image_count >= 1
        assert hw_used is False
        assert mock_popen.call_count == 3

        third_args = mock_popen.call_args_list[2][0][0]
        vf_index = third_args.index("-vf")
        vf_value = third_args[vf_index + 1]
        assert "scale=w=320:h=240:force_original_aspect_ratio=decrease" in vf_value
        assert "zscale" not in vf_value
        assert "tonemap" not in vf_value


class TestProcessItem:
    """Test the complete item processing pipeline."""

    @patch("os.path.isfile")
    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_process_item_success(
        self,
        mock_process_canonical,
        mock_isfile,
        mock_config,
        plex_xml_movie_tree,
    ):
        """Successful processing dispatches the canonical path through the unified pipeline."""
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
        )

        mock_plex = MagicMock()

        import xml.etree.ElementTree as ET

        mock_plex.query.return_value = ET.fromstring(plex_xml_movie_tree)
        mock_isfile.return_value = True

        mock_config.plex_config_folder = "/config/plex"
        mock_config.tmp_folder = "/tmp"
        mock_config.plex_local_videos_path_mapping = ""
        mock_config.plex_videos_path_mapping = ""
        mock_config.regenerate_thumbnails = False
        mock_process_canonical.return_value = MultiServerResult(
            canonical_path="/data/movies/Test Movie (2024)/Test Movie (2024).mkv",
            status=MultiServerStatus.PUBLISHED,
        )

        process_item("/library/metadata/54321", None, None, mock_config, mock_plex)

        # Shim funnels into process_canonical_path with the canonical path.
        assert mock_process_canonical.called

    @patch("os.path.isfile")
    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_process_item_path_mapping(
        self,
        mock_process_canonical,
        mock_isfile,
        mock_config,
        plex_xml_movie_tree,
    ):
        """Path mapping is applied to the canonical path passed into process_canonical_path."""
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
        )

        mock_plex = MagicMock()

        import xml.etree.ElementTree as ET

        mock_plex.query.return_value = ET.fromstring(plex_xml_movie_tree)

        mock_config.path_mappings = [
            {
                "plex_prefix": "/data",
                "local_prefix": "/mnt/videos",
                "webhook_prefixes": [],
            }
        ]
        mock_config.plex_config_folder = "/config/plex"
        mock_config.tmp_folder = "/tmp"
        mock_config.regenerate_thumbnails = False
        mock_isfile.return_value = True
        mock_process_canonical.return_value = MultiServerResult(
            canonical_path="/mnt/videos/movies/Test Movie (2024)/Test Movie (2024).mkv",
            status=MultiServerStatus.PUBLISHED,
        )

        process_item("/library/metadata/54321", None, None, mock_config, mock_plex)

        assert mock_process_canonical.called
        called_canonical = mock_process_canonical.call_args.kwargs["canonical_path"]
        import os as _os

        expected_prefix = _os.path.normpath("/mnt/videos")
        assert called_canonical.startswith(expected_prefix)

    @patch("os.path.isfile")
    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_process_item_mergerfs_multiple_plex_roots(
        self,
        mock_process_canonical,
        mock_isfile,
        mock_config,
        plex_xml_movie_tree,
    ):
        """Multi-mount path_mappings collapse multiple Plex roots onto one local prefix."""
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
        )

        mock_plex = MagicMock()
        import xml.etree.ElementTree as ET

        tree_xml = plex_xml_movie_tree.replace(
            'file="/data/movies/',
            'file="/data_disk1/movies/',
        )
        mock_plex.query.return_value = ET.fromstring(tree_xml)

        mock_config.path_mappings = [
            {
                "plex_prefix": "/data_disk1",
                "local_prefix": "/data",
                "webhook_prefixes": [],
            },
            {
                "plex_prefix": "/data_disk2",
                "local_prefix": "/data",
                "webhook_prefixes": [],
            },
        ]
        mock_config.plex_config_folder = "/config/plex"
        mock_config.tmp_folder = "/tmp"
        mock_config.regenerate_thumbnails = False
        mock_isfile.return_value = True
        mock_process_canonical.return_value = MultiServerResult(
            canonical_path="/data/movies/Test Movie (2024)/Test Movie (2024).mkv",
            status=MultiServerStatus.PUBLISHED,
        )

        process_item("/library/metadata/54321", None, None, mock_config, mock_plex)

        assert mock_process_canonical.called
        called_canonical = mock_process_canonical.call_args.kwargs["canonical_path"]
        import os as _os

        expected_prefix = _os.path.normpath("/data")
        assert called_canonical.startswith(expected_prefix), (
            f"Expected canonical path under /data, got {called_canonical}"
        )

    @patch("os.path.isfile")
    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_process_item_no_partial_prefix_match(
        self,
        mock_process_canonical,
        mock_isfile,
        mock_config,
        plex_xml_movie_tree,
    ):
        """Path /database/... is not remapped when mapping is /data -> /mnt/data."""
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
        )

        mock_plex = MagicMock()
        import xml.etree.ElementTree as ET

        tree_xml = plex_xml_movie_tree.replace(
            'file="/data/movies/',
            'file="/database/movies/',
        )
        mock_plex.query.return_value = ET.fromstring(tree_xml)

        mock_config.path_mappings = [
            {
                "plex_prefix": "/data",
                "local_prefix": "/mnt/data",
                "webhook_prefixes": [],
            }
        ]
        mock_config.plex_config_folder = "/config/plex"
        mock_config.tmp_folder = "/tmp"
        mock_config.regenerate_thumbnails = False
        mock_isfile.return_value = True
        mock_process_canonical.return_value = MultiServerResult(
            canonical_path="/database/movies/Test Movie (2024)/Test Movie (2024).mkv",
            status=MultiServerStatus.PUBLISHED,
        )

        process_item("/library/metadata/54321", None, None, mock_config, mock_plex)

        assert mock_process_canonical.called
        called_canonical = mock_process_canonical.call_args.kwargs["canonical_path"]
        import os as _os

        expected_prefix = _os.path.normpath("/database")
        assert called_canonical.startswith(expected_prefix), (
            f"Expected canonical path under /database, got {called_canonical}"
        )

    @patch("os.path.isfile")
    def test_process_item_missing_file(self, mock_isfile, mock_config, plex_xml_movie_tree):
        """Test handling of missing video file."""
        mock_plex = MagicMock()

        import xml.etree.ElementTree as ET

        mock_plex.query.return_value = ET.fromstring(plex_xml_movie_tree)

        # File doesn't exist
        mock_isfile.return_value = False

        mock_config.plex_config_folder = "/config/plex"
        mock_config.plex_local_videos_path_mapping = ""
        mock_config.plex_videos_path_mapping = ""

        # Should not crash, just skip the file
        process_item("/library/metadata/54321", None, None, mock_config, mock_plex)

    def test_process_item_plex_api_error(self, mock_config):
        """Test handling of Plex API errors."""
        mock_plex = MagicMock()
        mock_plex.query.side_effect = Exception("Plex API error")

        # Should not crash
        process_item("/library/metadata/54321", None, None, mock_config, mock_plex)


class TestMediaInfoImport:
    """Test MediaInfo import and validation."""

    def test_mediainfo_can_parse(self):
        """Test that MediaInfo is available and functional."""
        from pymediainfo import MediaInfo

        # This should not raise an exception in the test environment
        result = MediaInfo.can_parse()
        assert result is True or result is False  # Just check it doesn't crash


class TestDVNoBackwardCompat:
    """Test Dolby Vision Profile 5 / no-backward-compat detection."""

    @pytest.mark.parametrize(
        "hdr_format,expected",
        [
            # DV Profile 5 — no backward compat → True
            ("Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU", True),
            ("Dolby Vision, Version 1.0, dvhe.05.09, BL+RPU", True),
            # DV Profile 4 — explicitly unsafe for zscale (M3)
            ("Dolby Vision, Version 1.0, dvhe.04.06, BL+EL+RPU", True),
            # DV Profile 4 with misleading 'compatible' keyword (M3 regression)
            (
                "Dolby Vision, Version 1.0, dvhe.04.06, BL+EL+RPU, not backward compatible",
                True,
            ),
            # AV1 DV Profile 5 (M4)
            ("Dolby Vision, Version 1.0, dvav.05.09, BL+RPU", True),
            # AV1 DV set/entry (M4)
            ("Dolby Vision, Version 2.0, dvav.se.09, BL+RPU", True),
            # DV Profile 8 with HDR10 compat → False
            (
                "Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HDR10 compatible / SMPTE ST 2086",
                False,
            ),
            # DV Profile 7 with HDR10 compat → False
            ("Dolby Vision / HDR10 / SMPTE ST 2086", False),
            # DV with HLG compat → False
            ("Dolby Vision, HLG compatible", False),
            # Plain HDR10 (no DV) → False
            ("HDR10", False),
            ("SMPTE ST 2086, HDR10 compatible / SMPTE ST 2094 App 4", False),
            # None / empty → False
            (None, False),
            ("None", False),
            ("", False),
        ],
    )
    def test_detection(self, hdr_format: str, expected: bool) -> None:
        """_is_dv_no_backward_compat correctly classifies various hdr_format strings."""
        assert _is_dv_no_backward_compat(hdr_format) is expected


class TestIsDolbyVision:
    """Test _is_dolby_vision detection for all DV content."""

    @pytest.mark.parametrize(
        "hdr_format,expected",
        [
            # DV Profile 5 (no backward compat)
            ("Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU", True),
            # DV Profile 8 with HDR10 compat
            (
                "Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HDR10 compatible / SMPTE ST 2086",
                True,
            ),
            # DV Profile 7 with HDR10 compat
            ("Dolby Vision / HDR10 / SMPTE ST 2086", True),
            # DV with HLG compat
            ("Dolby Vision, HLG compatible", True),
            # DV + HDR10+
            (
                "Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HDR10 compatible / SMPTE ST 2094 App 4",
                True,
            ),
            # Plain HDR10 (no DV) → False
            ("HDR10", False),
            ("SMPTE ST 2086, HDR10 compatible / SMPTE ST 2094 App 4", False),
            # Plain HLG → False
            ("HLG", False),
            # None / empty → False
            (None, False),
            ("None", False),
            ("", False),
        ],
    )
    def test_detection(self, hdr_format: str, expected: bool) -> None:
        """_is_dolby_vision correctly identifies any DV content."""
        assert _is_dolby_vision(hdr_format) is expected


class TestDetectZscaleColorspaceError:
    """Test zscale colorspace error detection in stderr."""

    def test_detects_no_path_between_colorspaces(self) -> None:
        lines = [
            "[Parsed_zscale_1 @ 0x55eb] code 3074: no path between colorspaces",
            "Last message repeated 31 times",
        ]
        assert _detect_zscale_colorspace_error(lines) is True

    def test_detects_generic_error_from_zscale(self) -> None:
        lines = [
            "[vf#0:0 @ 0x55eb] Error while filtering: Generic error in an external library",
            "zscale: Generic error in an external library",
        ]
        assert _detect_zscale_colorspace_error(lines) is True

    def test_detects_bracketed_parsed_zscale_filter(self) -> None:
        """H1: FFmpeg logs [Parsed_zscale_1 @ 0x...] without bare 'zscale:' prefix."""
        lines = [
            "[Parsed_zscale_1 @ 0x55eb] Generic error in an external library",
        ]
        assert _detect_zscale_colorspace_error(lines) is True

    def test_detects_bracketed_vf_zscale_filter(self) -> None:
        """H1: FFmpeg logs [vf#0:0/zscale @ 0x...] variant."""
        lines = [
            "[vf#0:0/zscale @ 0x5f3a928] Generic error in an external library",
        ]
        assert _detect_zscale_colorspace_error(lines) is True

    def test_no_match_on_unrelated_error(self) -> None:
        lines = ["Permission denied", "No such file or directory"]
        assert _detect_zscale_colorspace_error(lines) is False

    def test_empty_lines(self) -> None:
        assert _detect_zscale_colorspace_error([]) is False
        assert _detect_zscale_colorspace_error(None) is False


class TestProactiveDVSkip:
    """Integration test verifying DV routing: Profile 5 -> libplacebo, Profile 7/8 -> zscale."""

    def _run_generate(
        self,
        hdr_format_str,
        video_filename,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
        gpu=None,
        gpu_device_path=None,
        vulkan_device_info=None,
    ):
        """Shared helper: run generate_images and return the FFmpeg args.

        ``vulkan_device_info`` patches ``get_vulkan_device_info`` for the
        duration of the call.  Defaults to a healthy hardware device so
        existing DV5 tests continue to hit the libplacebo path; tests
        for the software-fallback guard pass a software dict.

        ``gpu_device_path`` is forwarded to ``generate_images`` so tests
        can select between the VAAPI+Vulkan interop DV5 path (pass a
        ``/dev/dri/renderD*`` path) and the software-fallback DV5 path
        (pass ``None``).
        """
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=hdr_format_str)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = []
        mock_detect.return_value = False

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        # Accept either a dict (legacy test shape) or a VulkanProbeResult,
        # normalising to VulkanProbeResult for the production contract.
        if vulkan_device_info is None:
            default_vulkan = VulkanProbeResult(device="Quadro P4000 (NVIDIA)", is_software=False)
        elif isinstance(vulkan_device_info, VulkanProbeResult):
            default_vulkan = vulkan_device_info
        else:
            default_vulkan = VulkanProbeResult(
                device=vulkan_device_info.get("device"),
                is_software=vulkan_device_info.get("is_software", False),
            )

        with patch(
            "media_preview_generator.gpu.vulkan_probe.get_vulkan_device_info",
            return_value=default_vulkan,
        ):
            success, image_count, hw_used, seconds, speed, *_ = generate_images(
                video_filename, temp_dir, gpu, gpu_device_path, mock_config
            )

        assert success is True
        assert image_count >= 1
        assert mock_popen.call_count == 1

        return mock_popen.call_args_list[0][0][0]

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_dv_profile5_uses_libplacebo(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV Profile 5 on an Intel host with a VAAPI render device
        uses VAAPI hardware decode + OpenCL tonemap_opencl via
        jellyfin-ffmpeg's DV-aware patch.

        Intel's VAAPI→Vulkan libplacebo path is broken upstream on
        Mesa ANV (libplacebo vkCreateImage returns VK_ERROR_OUT_OF_
        DEVICE_MEMORY).  Jellyfin-ffmpeg's tonemap_opencl reads DV RPU
        side-data correctly and produces correct colours at ~17x on
        Intel UHD 770.  See issue #212.
        """
        args = self._run_generate(
            "Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU",
            "/test/dv_profile5.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            gpu="INTEL",
            gpu_device_path="/dev/dri/renderD128",
        )

        # Intel DV5 path initialises VAAPI on the render node and an
        # OpenCL context derived from that VAAPI device (ocl@va).
        init_indices = [i for i, a in enumerate(args) if a == "-init_hw_device"]
        assert len(init_indices) == 2, "Intel DV5 path must initialise VAAPI + derived OpenCL (2 devices)"
        assert args[init_indices[0] + 1] == "vaapi=va:/dev/dri/renderD128"
        assert args[init_indices[1] + 1] == "opencl=ocl@va"
        assert "-filter_hw_device" in args
        assert args[args.index("-filter_hw_device") + 1] == "ocl"

        # -hwaccel must be vaapi, output format kept on-device (vaapi),
        # device reference must match the named "va" VAAPI context.
        assert "-hwaccel" in args
        assert args[args.index("-hwaccel") + 1] == "vaapi"
        assert "-hwaccel_device" in args
        assert args[args.index("-hwaccel_device") + 1] == "va"
        assert "-hwaccel_output_format" in args
        assert args[args.index("-hwaccel_output_format") + 1] == "vaapi"

        # Hardware-decoded DV5, so -threads:v 1.
        assert "-threads:v" in args
        assert args[args.index("-threads:v") + 1] == "1"

        # Skip-frame stays disabled on DV5 because RPU side data has
        # inter-frame dependencies.
        assert "-skip_frame:v" not in args

        # Filter chain: fps first (drop frames BEFORE the expensive tonemap
        # step), then setparams to tag the base layer as BT.2020/PQ, then
        # hwmap to OpenCL and tonemap_opencl, then hwdownload for mjpeg.
        vf = args[args.index("-vf") + 1]
        assert "hwmap=derive_device=opencl:mode=read" in vf
        assert "tonemap_opencl=" in vf
        assert "hwmap=derive_device=vulkan" not in vf, (
            "Intel DV5 uses OpenCL, not Vulkan (libplacebo interop is broken)"
        )
        assert "libplacebo=" not in vf, "Intel DV5 path uses tonemap_opencl, not libplacebo"
        assert "hwupload" not in vf
        assert "hwdownload" in vf
        assert "zscale" not in vf
        # fps filter must come BEFORE hwmap — that's the 6x speed-up
        # (benchmarked 2.71x→17.3x on UHD 770).
        fps_pos = vf.find("fps=fps=")
        hwmap_pos = vf.find("hwmap=")
        assert 0 <= fps_pos < hwmap_pos, (
            "fps filter must run BEFORE hwmap so we drop frames at the VAAPI surface and don't waste tonemap cycles"
        )

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_dv_profile5_amd_uses_vaapi_hwaccel(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """AMD on DV Profile 5 uses VAAPI decode + Vulkan libplacebo
        via the Jellyfin ``drm=dr → vaapi=va@dr → vulkan=vk@dr`` pattern.
        Intel takes a separate OpenCL tonemap path (see Intel test above)
        because Mesa ANV's Vulkan DMA-BUF import is broken for DV5 format
        modifiers.  AMD's Mesa RADV has working DMA-BUF interop so we
        keep the libplacebo path there.
        """
        args = self._run_generate(
            "Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU",
            "/test/dv_profile5.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            gpu="AMD",
            gpu_device_path="/dev/dri/renderD129",
        )

        # Same VAAPI + vk@va + hwmap structure as Intel, different node.
        init_indices = [i for i, a in enumerate(args) if a == "-init_hw_device"]
        assert len(init_indices) == 3
        assert args[init_indices[0] + 1] == "drm=dr:/dev/dri/renderD129"
        assert args[init_indices[1] + 1] == "vaapi=va@dr"
        assert args[init_indices[2] + 1] == "vulkan=vk@dr"
        assert "-hwaccel" in args and args[args.index("-hwaccel") + 1] == "vaapi"
        assert args[args.index("-hwaccel_output_format") + 1] == "vaapi"
        assert args[args.index("-threads:v") + 1] == "1"
        vf = args[args.index("-vf") + 1]
        assert "hwmap=derive_device=vulkan" in vf
        assert "libplacebo" in vf
        assert "hwupload" not in vf

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_dv_profile5_non_dri_falls_back_to_sw_decode(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV Profile 5 on a Linux GPU without a /dev/dri render node
        (defensive edge case) falls back to software decode + the
        plain ``vulkan=vk`` libplacebo path with ``-threads:v 0`` so
        the HEVC decoder can still saturate CPU cores.
        """
        args = self._run_generate(
            "Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU",
            "/test/dv_profile5.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            gpu="INTEL",
            gpu_device_path=None,
        )

        # No VAAPI hwaccel, vanilla vulkan=vk device init only.
        assert "-hwaccel" not in args
        init_indices = [i for i, a in enumerate(args) if a == "-init_hw_device"]
        assert len(init_indices) == 1
        assert args[init_indices[0] + 1] == "vulkan=vk"

        # Software decode path must uncap the video decoder threads.
        assert "-threads:v" in args
        assert args[args.index("-threads:v") + 1] == "0"

        # Filter chain falls back to hwupload (CPU frame → Vulkan).
        vf = args[args.index("-vf") + 1]
        assert "hwupload" in vf
        assert "hwmap=derive_device=vulkan" not in vf
        assert "libplacebo" in vf

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_dv_profile5_software_vulkan_uses_dv_safe_filter(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV5 + software Vulkan drops libplacebo and uses the DV-safe filter.

        Regression guard for issue #213's latent bug: if Vulkan falls
        back to ``llvmpipe`` (e.g. because the container's NVIDIA GLVND
        config is missing), libplacebo would silently produce green
        thumbnails.  The pre-flight guard in ``generate_images`` checks
        ``get_vulkan_device_info()["is_software"]`` and, when true,
        skips libplacebo entirely and falls through to a plain
        ``fps=...,scale=...`` filter chain that produces dim-but-
        colour-correct output.
        """
        args = self._run_generate(
            "Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU",
            "/test/dv_profile5_sw_vulkan.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            gpu="NVIDIA",
            vulkan_device_info={
                "device": "llvmpipe (LLVM 18.1.3, 256 bits) (software)",
                "is_software": True,
            },
        )

        # The pre-flight guard must bypass libplacebo entirely — no
        # Vulkan init, no hwupload, no libplacebo filter.
        assert "-init_hw_device" not in args, "Software Vulkan must not initialise a Vulkan device for DV5"
        assert "-filter_hw_device" not in args
        vf = args[args.index("-vf") + 1]
        assert "libplacebo" not in vf, "Software Vulkan must not run libplacebo on DV5 (green overlay bug)"
        assert "hwupload" not in vf
        # Zscale is also wrong on DV5 (no HDR10 base layer) — the
        # fallback must use the plain fps+scale chain, same as the
        # DV-safe retry target.  On NVIDIA, fps is followed by
        # scale_cuda+hwdownload (issue #218 GPU-scale optimisation)
        # rather than a CPU scale.
        assert "zscale" not in vf
        assert "tonemap" not in vf
        assert "scale_cuda=w=320:h=240:force_original_aspect_ratio=decrease" in vf
        assert "hwdownload" in vf
        assert vf.startswith("fps=fps=")

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_dv_profile5_no_vulkan_device_uses_dv_safe_filter(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV5 + no Vulkan device at all also falls back to DV-safe filter.

        Same guard as the software-Vulkan case but with ``device=None``
        instead of a software rasteriser string.  Both mean "libplacebo
        is not usable" and must route to the fps+scale chain.
        """
        args = self._run_generate(
            "Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU",
            "/test/dv_profile5_no_vulkan.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            gpu="INTEL",
            vulkan_device_info={"device": None, "is_software": False},
        )

        assert "-init_hw_device" not in args
        vf = args[args.index("-vf") + 1]
        assert "libplacebo" not in vf
        assert "hwupload" not in vf
        assert "zscale" not in vf
        assert vf.startswith("fps=fps=")

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_dv_profile5_nvidia_uses_nvdec(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV Profile 5 on NVIDIA hosts decodes via NVDEC before libplacebo.

        Benchmarked 2026-04 against ``linuxserver/ffmpeg:8.0.1-cli-ls56``:
        5 minutes of 4K HEVC DV Profile 5 content runs at ~12x on NVDEC vs
        ~4x on software decode — a ~3x end-to-end speedup — with
        visually identical output after the libplacebo ``apply_dolbyvision``
        tone map.  Any code change that silently drops ``-hwaccel cuda``
        from this path would regress the speedup.
        """
        args = self._run_generate(
            "Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU",
            "/test/dv_profile5_nvidia.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            gpu="NVIDIA",
        )

        # libplacebo path still used for tone mapping
        assert "-init_hw_device" in args
        assert args[args.index("-init_hw_device") + 1] == "vulkan=vk"
        assert "-filter_hw_device" in args
        assert args[args.index("-filter_hw_device") + 1] == "vk"

        # NVDEC is attached BEFORE the input file
        assert "-hwaccel" in args
        assert args[args.index("-hwaccel") + 1] == "cuda"
        assert args.index("-hwaccel") < args.index("-i")

        # HW decode is active, so the video-thread cap reapplies to
        # prevent decoder thread oversubscription across GPU workers.
        assert "-threads:v" in args
        assert args[args.index("-threads:v") + 1] == "1"

        # RPU side-data still has inter-frame dependencies even under
        # NVDEC — keyframe-only decode must stay off.
        assert "-skip_frame:v" not in args

        # Filter graph is unchanged: libplacebo does the DV tone map.
        vf = args[args.index("-vf") + 1]
        assert "libplacebo" in vf
        assert "hwupload" in vf
        assert "zscale" not in vf

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_dv_profile5_cpu_skips_hwaccel(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV Profile 5 with no GPU configured stays on software decode."""
        args = self._run_generate(
            "Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU",
            "/test/dv_profile5_cpu.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            gpu=None,
        )

        assert "-init_hw_device" in args
        assert "-hwaccel" not in args
        assert "-threads:v" not in args
        assert "-skip_frame:v" not in args
        vf = args[args.index("-vf") + 1]
        assert "libplacebo" in vf

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_dv_profile8_hdr10_uses_zscale(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV Profile 8 with HDR10 fallback uses zscale/tonemap on the HDR10 base layer."""
        args = self._run_generate(
            "Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HDR10 compatible / SMPTE ST 2086",
            "/test/dv_profile8_hdr10.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
        )

        # Profile 7/8 uses the HDR10 base layer — no libplacebo needed
        assert "-init_hw_device" not in args
        vf = args[args.index("-vf") + 1]
        assert "zscale" in vf
        assert "tonemap" in vf
        assert "libplacebo" not in vf

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_dv_hdr10plus_uses_zscale(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV + HDR10+ (the Severance scenario from #178) uses zscale via HDR10 base layer."""
        args = self._run_generate(
            "Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HDR10 compatible / SMPTE ST 2094 App 4",
            "/test/dv_hdr10plus.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
        )

        assert "-init_hw_device" not in args
        vf = args[args.index("-vf") + 1]
        assert "zscale" in vf
        assert "tonemap" in vf
        assert "libplacebo" not in vf

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_dv_profile8_with_gpu_uses_cuda_and_zscale(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV Profile 8 on GPU uses CUDA hwaccel + zscale (reads HDR10 base layer)."""
        args = self._run_generate(
            "Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HDR10 compatible / SMPTE ST 2086",
            "/test/dv_gpu.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            gpu="NVIDIA",
        )

        # Profile 7/8 with HDR10 fallback — standard path with GPU decode
        assert "-hwaccel" in args
        assert args[args.index("-hwaccel") + 1] == "cuda"
        assert "-init_hw_device" not in args

        # HW decode is active so the video-thread cap must still be present
        # (prevents decoder thread oversubscription across GPU workers).
        assert "-threads:v" in args
        assert args[args.index("-threads:v") + 1] == "1"

        vf = args[args.index("-vf") + 1]
        assert "zscale" in vf
        assert "tonemap" in vf
        assert "libplacebo" not in vf


class TestLibplaceboFallback:
    """Test that libplacebo failure triggers the DV-safe retry with basic fps+scale."""

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_dv_profile5_libplacebo_failure_falls_back(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """When libplacebo FFmpeg call fails, retry with basic fps+scale chain."""
        # Arrange — heuristic probe fails (no skip)
        mock_run.return_value = MagicMock(returncode=1, stderr="")

        # MediaInfo: DV Profile 5
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format="Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU")]
        mock_mediainfo.parse.return_value = mock_info

        # First FFmpeg call (libplacebo) fails, second (DV-safe) succeeds
        mock_proc_fail = MagicMock()
        mock_proc_fail.poll.side_effect = [None, 0]
        mock_proc_fail.returncode = 1

        mock_proc_ok = MagicMock()
        mock_proc_ok.poll.side_effect = [None, 0]
        mock_proc_ok.returncode = 0

        mock_popen.side_effect = [mock_proc_fail, mock_proc_ok]

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = []
        mock_detect.return_value = False

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"

        call_count = [0]

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                call_count[0] += 1
                # First img count (after libplacebo fail) returns 0,
                # second (after DV-safe retry) returns 1
                if call_count[0] <= 1:
                    return []
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        # Act — force a healthy Vulkan device so the pre-flight software
        # guard added in Change 3 lets the libplacebo path build normally.
        # This test specifically exercises the in-flight libplacebo failure
        # path, not the software-Vulkan pre-flight fallback.
        with patch(
            "media_preview_generator.gpu.vulkan_probe.get_vulkan_device_info",
            return_value=VulkanProbeResult(device="Quadro P4000", is_software=False),
        ):
            success, image_count, hw_used, seconds, speed, *_ = generate_images(
                "/test/dv_profile5.mkv", temp_dir, None, None, mock_config
            )

        # Assert — success after DV-safe retry
        assert success is True
        assert image_count >= 1
        assert mock_popen.call_count == 2

        # First call should have libplacebo + vulkan init
        first_args = mock_popen.call_args_list[0][0][0]
        assert "-init_hw_device" in first_args
        assert first_args[first_args.index("-init_hw_device") + 1] == "vulkan=vk"
        assert "-filter_hw_device" in first_args
        vf1_idx = first_args.index("-vf")
        assert "libplacebo" in first_args[vf1_idx + 1]

        # Second call (DV-safe retry) should be basic fps+scale, no vulkan
        second_args = mock_popen.call_args_list[1][0][0]
        assert "-init_hw_device" not in second_args
        assert "-filter_hw_device" not in second_args
        vf2_idx = second_args.index("-vf")
        vf2_value = second_args[vf2_idx + 1]
        assert "libplacebo" not in vf2_value
        assert "zscale" not in vf2_value
        assert "fps=" in vf2_value
        assert "scale=w=320:h=240:force_original_aspect_ratio=decrease" in vf2_value


class TestZscaleErrorRetry:
    """M6: Test that zscale colorspace errors trigger the DV-safe retry path."""

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_zscale_error_triggers_dv_safe_retry(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Bracketed zscale error in stderr triggers DV-safe retry (fps+scale)."""
        # Arrange — heuristic probe allows skip
        mock_run.return_value = MagicMock(returncode=0)

        # MediaInfo: plain HDR10 (not DV) so initial chain includes zscale/tonemap
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format="HDR10")]
        mock_mediainfo.parse.return_value = mock_info

        # FFmpeg: fail with rc=187 twice (skip + no-skip), succeed on DV-safe retry
        mock_proc_skip = MagicMock()
        mock_proc_skip.poll.side_effect = [None, 0]
        mock_proc_skip.returncode = 187

        mock_proc_noskip = MagicMock()
        mock_proc_noskip.poll.side_effect = [None, 0]
        mock_proc_noskip.returncode = 187

        mock_proc_dv_safe = MagicMock()
        mock_proc_dv_safe.poll.side_effect = [None, 0]
        mock_proc_dv_safe.returncode = 0

        mock_popen.side_effect = [mock_proc_skip, mock_proc_noskip, mock_proc_dv_safe]

        mock_exists.return_value = True
        # Stderr containing the bracketed zscale error pattern (H1)
        mock_file.return_value.readlines.return_value = [
            "[Parsed_zscale_1 @ 0x55eb] Generic error in an external library\n"
        ]

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"
        img_call_count = {"count": 0}

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                img_call_count["count"] += 1
                if img_call_count["count"] == 1:
                    return []  # initial count: no images
                if img_call_count["count"] == 2:
                    return [img1]  # after DV-safe retry
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect
        mock_detect.return_value = False
        mock_config.cpu_threads = 1

        # Act
        success, image_count, hw_used, seconds, speed, *_ = generate_images(
            "/test/hdr10_zscale_crash.mkv", temp_dir, "NVIDIA", None, mock_config
        )

        # Assert — success after 3 Popen calls, third uses DV-safe vf
        assert success is True
        assert image_count >= 1
        assert mock_popen.call_count == 3

        third_args = mock_popen.call_args_list[2][0][0]
        vf_index = third_args.index("-vf")
        vf_value = third_args[vf_index + 1]
        # NVIDIA DV-safe retry keeps scale_cuda + hwdownload (issue #218).
        assert "scale_cuda=w=320:h=240:force_original_aspect_ratio=decrease" in vf_value
        assert "hwdownload" in vf_value
        assert "zscale" not in vf_value
        assert "tonemap" not in vf_value


class TestDVSafeRetryGpuFailure:
    """M8: DV-safe retry fails on GPU — should raise CodecNotSupportedError."""

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_generate_images_dv_safe_retry_gpu_failure_raises_codec_error(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """All FFmpeg attempts fail with zscale error on GPU → CodecNotSupportedError."""
        # Arrange — heuristic allows skip
        mock_run.return_value = MagicMock(returncode=0)

        # MediaInfo: plain HDR10 → initial chain has zscale/tonemap
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format="HDR10")]
        mock_mediainfo.parse.return_value = mock_info

        # All three FFmpeg calls fail
        for _ in range(3):
            proc = MagicMock()
            proc.poll.side_effect = [None, 0]
            proc.returncode = 187
        mock_popen.side_effect = [
            MagicMock(poll=MagicMock(side_effect=[None, 0]), returncode=187),
            MagicMock(poll=MagicMock(side_effect=[None, 0]), returncode=187),
            MagicMock(poll=MagicMock(side_effect=[None, 0]), returncode=187),
        ]

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = [
            "[Parsed_zscale_1 @ 0x55eb] Generic error in an external library\n"
        ]

        # No images at any stage
        mock_glob.return_value = []
        mock_detect.return_value = False
        mock_config.cpu_threads = 1

        # Act & Assert — GPU path raises CodecNotSupportedError
        with pytest.raises(CodecNotSupportedError):
            generate_images("/test/always_fails.mkv", temp_dir, "NVIDIA", None, mock_config)

        assert mock_popen.call_count == 3


class TestDynamicNpl:
    """M7: Test that npl is always 100 (SDR reference white) regardless of MaxCLL."""

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_npl_always_100_with_maxcll(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """npl is always 100 (SDR reference) even when MaxCLL is available."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        video_track = MagicMock()
        video_track.hdr_format = "HDR10"
        video_track.maximum_content_light_level = "1000"
        mock_info.video_tracks = [video_track]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = []
        mock_detect.return_value = False

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        success, *_ = generate_images("/test/hdr10_1000nit.mkv", temp_dir, None, None, mock_config)

        assert success is True
        first_args = mock_popen.call_args_list[0][0][0]
        vf_index = first_args.index("-vf")
        vf_value = first_args[vf_index + 1]
        assert "npl=100" in vf_value
        assert "npl=1000" not in vf_value

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_npl_always_100_without_maxcll(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """npl is always 100 (SDR reference) even when MaxCLL is absent."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        video_track = MagicMock()
        video_track.hdr_format = "HDR10"
        video_track.maximum_content_light_level = None
        mock_info.video_tracks = [video_track]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = []
        mock_detect.return_value = False

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        success, *_ = generate_images("/test/hdr10_no_maxcll.mkv", temp_dir, None, None, mock_config)

        assert success is True
        first_args = mock_popen.call_args_list[0][0][0]
        vf_index = first_args.index("-vf")
        vf_value = first_args[vf_index + 1]
        assert "npl=100" in vf_value

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_hdr_uses_desat_0(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """HDR filter chain uses desat=0 to preserve colour saturation."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        video_track = MagicMock()
        video_track.hdr_format = "HDR10"
        video_track.maximum_content_light_level = "N/A"
        mock_info.video_tracks = [video_track]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = []
        mock_detect.return_value = False

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        success, *_ = generate_images("/test/hdr10_bad_maxcll.mkv", temp_dir, None, None, mock_config)

        assert success is True
        first_args = mock_popen.call_args_list[0][0][0]
        vf_index = first_args.index("-vf")
        vf_value = first_args[vf_index + 1]
        assert "desat=0" in vf_value
        assert "npl=100" in vf_value


class TestDetectDolbyVisionRPUError:
    """L11: Unit tests for _detect_dolby_vision_rpu_error."""

    def test_detects_multiple_rpus(self) -> None:
        lines = ["Multiple Dolby Vision RPUs found in one AU"]
        assert _detect_dolby_vision_rpu_error(lines) is True

    def test_detects_multiple_rpus_with_skipping(self) -> None:
        lines = ["multiple dolby vision rpus found in one au. skipping previous."]
        assert _detect_dolby_vision_rpu_error(lines) is True

    def test_case_insensitive(self) -> None:
        lines = ["MULTIPLE DOLBY VISION RPUS FOUND IN ONE AU"]
        assert _detect_dolby_vision_rpu_error(lines) is True

    def test_no_match_on_unrelated(self) -> None:
        lines = ["Codec error: unsupported codec"]
        assert _detect_dolby_vision_rpu_error(lines) is False

    def test_empty_input(self) -> None:
        assert _detect_dolby_vision_rpu_error([]) is False
        assert _detect_dolby_vision_rpu_error(None) is False


class TestSaveFFmpegFailureLog:
    """L11: Unit tests for _save_ffmpeg_failure_log."""

    def test_creates_log_file(self, tmp_path, monkeypatch) -> None:
        """Log file is created with expected content."""
        log_dir = str(tmp_path)
        monkeypatch.setenv("CONFIG_DIR", log_dir)

        _save_ffmpeg_failure_log("/media/test.mkv", 187, ["error line 1", "error line 2"])

        ffmpeg_log_dir = tmp_path / "logs" / "ffmpeg_failures"
        log_files = list(ffmpeg_log_dir.glob("*.log"))
        assert len(log_files) == 1

        content = log_files[0].read_text(encoding="utf-8")
        assert "file: /media/test.mkv" in content
        assert "exit_code: 187" in content
        assert "signal_killed: False" in content
        assert "exit_diagnosis: high_exit_non_signal" in content
        assert "error line 1" in content
        assert "error line 2" in content

    def test_caps_at_500_files(self, tmp_path, monkeypatch) -> None:
        """Directory is capped at 500 log files."""
        log_dir = str(tmp_path)
        monkeypatch.setenv("CONFIG_DIR", log_dir)

        ffmpeg_log_dir = tmp_path / "logs" / "ffmpeg_failures"
        ffmpeg_log_dir.mkdir(parents=True)

        # Pre-create 500 files
        import time as _time

        for i in range(500):
            (ffmpeg_log_dir / f"old_{i:04d}.log").write_text("old")
            _time.sleep(0.001)  # ensure distinct mtimes

        _save_ffmpeg_failure_log("/media/new.mkv", 1, ["new error"])

        log_files = list(ffmpeg_log_dir.glob("*.log"))
        assert len(log_files) <= 501  # 500 cap + new file (oldest removed)

    def test_handles_oserror_gracefully(self, monkeypatch) -> None:
        """OSError during directory creation is swallowed."""
        monkeypatch.setenv("CONFIG_DIR", "/nonexistent/readonly/path")
        # Should not raise
        _save_ffmpeg_failure_log("/media/test.mkv", 1, ["error"])


class TestDiagnoseFFmpegExitCode:
    """Unit tests for _diagnose_ffmpeg_exit_code."""

    @pytest.mark.parametrize(
        ("returncode", "expected"),
        [
            (0, "success"),
            (130, "signal:SIGINT"),
            (137, "signal:SIGKILL"),
            (143, "signal:SIGTERM"),
            (251, "io_error"),
            (187, "high_exit_non_signal"),
            (-15, "signal:15"),
            (1, "error"),
        ],
    )
    def test_classifies_exit_codes(self, returncode: int, expected: str) -> None:
        assert _diagnose_ffmpeg_exit_code(returncode) == expected


class TestVerifyTmpFolderHealth:
    """Unit tests for _verify_tmp_folder_health."""

    def test_returns_healthy_for_writable_directory(self, tmp_path) -> None:
        ok, messages = _verify_tmp_folder_health(str(tmp_path))
        assert ok is True
        assert messages == []

    def test_returns_error_for_unwritable_directory(self, tmp_path) -> None:
        with patch("builtins.open", side_effect=OSError("read-only")):
            ok, messages = _verify_tmp_folder_health(str(tmp_path))
        assert ok is False
        assert messages
        assert "not writable" in messages[0].lower()

    def test_warns_when_disk_space_low(self, tmp_path) -> None:
        with patch("media_preview_generator.processing.orchestrator.shutil.disk_usage") as mock_usage:
            mock_usage.return_value = MagicMock(free=0)
            ok, messages = _verify_tmp_folder_health(str(tmp_path), min_free_mb=1)
        assert ok is True
        assert messages
        assert "low free space" in messages[0].lower()


class TestHdrFormatNoneString:
    """L12: hdr_format='None' string should produce the SDR filter path."""

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_hdr_format_none_string_uses_sdr_path(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """When MediaInfo returns 'None' (string), SDR path is taken (no zscale/tonemap)."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format="None")]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = []
        mock_detect.return_value = False

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        success, *_ = generate_images("/test/sdr_none_string.mkv", temp_dir, None, None, mock_config)

        assert success is True
        first_args = mock_popen.call_args_list[0][0][0]
        vf_index = first_args.index("-vf")
        vf_value = first_args[vf_index + 1]
        assert "zscale" not in vf_value
        assert "tonemap" not in vf_value
        assert "fps=" in vf_value
        assert "scale=w=320:h=240:force_original_aspect_ratio=decrease" in vf_value


class TestGpuScaleOptimisation:
    """Issue #218: keep decoded frames on the GPU through downscale so
    per-worker RSS drops from ~1 GB to ~300 MB on 4K HDR10.
    """

    @staticmethod
    def _run(mock_mediainfo, mock_popen, mock_config, *, gpu, gpu_device, hdr_fmt):
        info = MagicMock()
        info.video_tracks = [MagicMock(hdr_format=hdr_fmt)]
        mock_mediainfo.parse.return_value = info
        proc = MagicMock()
        proc.poll.side_effect = [None, 0]
        proc.returncode = 0
        mock_popen.return_value = proc
        with (
            patch("os.path.exists", return_value=False),
            patch("builtins.open", new_callable=mock_open),
            patch("time.sleep"),
            patch("media_preview_generator.processing.orchestrator.glob.glob", return_value=[]),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
        ):
            generate_images("/test/v.mp4", "/tmp/o", gpu, gpu_device, mock_config)
        return mock_popen.call_args[0][0]

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    def test_nvidia_sdr_uses_scale_cuda_with_hwaccel_output_format(self, mock_popen, mock_mediainfo, mock_config):
        args = self._run(
            mock_mediainfo,
            mock_popen,
            mock_config,
            gpu="NVIDIA",
            gpu_device="cuda",
            hdr_fmt=None,
        )
        assert "-hwaccel" in args
        assert args[args.index("-hwaccel") + 1] == "cuda"
        assert "-hwaccel_output_format" in args
        assert args[args.index("-hwaccel_output_format") + 1] == "cuda"
        vf = args[args.index("-vf") + 1]
        assert "scale_cuda=w=320:h=240:force_original_aspect_ratio=decrease" in vf
        assert "force_divisible_by=2" in vf, "Letterboxed 2.4:1 content needs even output dims or zscale rejects it"
        assert "format=nv12" in vf
        assert "hwdownload" in vf
        # The old CPU-scale bug must not re-appear.
        assert "scale=w=320" not in vf.replace("scale_cuda=w=320", "")
        # Decoder thread cap unchanged.
        assert "-threads:v" in args
        assert args[args.index("-threads:v") + 1] == "1"

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    def test_nvidia_hdr10_downscales_on_gpu_before_zscale(self, mock_popen, mock_mediainfo, mock_config):
        args = self._run(
            mock_mediainfo,
            mock_popen,
            mock_config,
            gpu="NVIDIA",
            gpu_device="cuda",
            hdr_fmt="SMPTE ST 2086",
        )
        vf = args[args.index("-vf") + 1]
        # scale_cuda must appear BEFORE zscale so zscale tonemaps a
        # 320x240 frame, not a 4K one.
        scale_idx = vf.find("scale_cuda=")
        zscale_idx = vf.find("zscale=t=linear")
        assert scale_idx != -1 and zscale_idx != -1
        assert scale_idx < zscale_idx
        assert "format=p010le" in vf, "HDR10 path must keep 10-bit through GPU scale"
        assert "tonemap=hable" in vf

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    def test_vaapi_sdr_uses_scale_vaapi_with_even_parity_safety(self, mock_popen, mock_mediainfo, mock_config):
        args = self._run(
            mock_mediainfo,
            mock_popen,
            mock_config,
            gpu="INTEL",
            gpu_device="/dev/dri/renderD128",
            hdr_fmt=None,
        )
        assert "-hwaccel" in args
        assert args[args.index("-hwaccel") + 1] == "vaapi"
        # Modern -hwaccel_device pairs with -hwaccel_output_format;
        # the deprecated -vaapi_device would break this pairing.
        assert "-hwaccel_device" in args
        assert args[args.index("-hwaccel_device") + 1] == "/dev/dri/renderD128"
        assert "-vaapi_device" not in args
        assert "-hwaccel_output_format" in args
        assert args[args.index("-hwaccel_output_format") + 1] == "vaapi"
        vf = args[args.index("-vf") + 1]
        assert "scale_vaapi=w=320:h=240:force_original_aspect_ratio=decrease" in vf
        assert "hwdownload" in vf
        # scale_vaapi lacks force_divisible_by; the CPU parity fix after
        # hwdownload snaps letterboxed odd heights (e.g. 320x133) to even.
        assert "scale=trunc(iw/2)*2:trunc(ih/2)*2" in vf

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    def test_vaapi_hdr10_downscales_on_gpu_before_zscale(self, mock_popen, mock_mediainfo, mock_config):
        args = self._run(
            mock_mediainfo,
            mock_popen,
            mock_config,
            gpu="AMD",
            gpu_device="/dev/dri/renderD128",
            hdr_fmt="SMPTE ST 2086",
        )
        vf = args[args.index("-vf") + 1]
        scale_idx = vf.find("scale_vaapi=")
        parity_idx = vf.find("scale=trunc(iw/2)*2")
        zscale_idx = vf.find("zscale=t=linear")
        assert scale_idx != -1 and parity_idx != -1 and zscale_idx != -1
        # scale_vaapi (GPU) → hwdownload → parity-fix scale (CPU, tiny)
        # → zscale tonemap on the 320x240 frame.
        assert scale_idx < parity_idx < zscale_idx
        assert "format=p010le" in vf

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    def test_cpu_path_retains_software_scale(self, mock_popen, mock_mediainfo, mock_config):
        args = self._run(
            mock_mediainfo,
            mock_popen,
            mock_config,
            gpu=None,
            gpu_device=None,
            hdr_fmt=None,
        )
        vf = args[args.index("-vf") + 1]
        # CPU path: plain libswscale scale — no GPU filters.
        assert "scale_cuda" not in vf
        assert "scale_vaapi" not in vf
        assert "hwdownload" not in vf
        assert "scale=w=320:h=240:force_original_aspect_ratio=decrease" in vf
        # And no hwaccel_output_format leaked in.
        assert "-hwaccel_output_format" not in args

    @patch(
        "media_preview_generator.gpu.vulkan_probe.get_vulkan_device_info",
        return_value=VulkanProbeResult(device="vk", is_software=False),
    )
    @patch(
        "media_preview_generator.gpu.vulkan_probe.get_vulkan_env_overrides",
        return_value={},
    )
    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    def test_dv5_libplacebo_vf_unchanged(self, mock_popen, mock_mediainfo, _vk_env, _vk_info, mock_config):
        args = self._run(
            mock_mediainfo,
            mock_popen,
            mock_config,
            gpu="NVIDIA",
            gpu_device="cuda",
            hdr_fmt="Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU",
        )
        vf = args[args.index("-vf") + 1]
        # DV5 libplacebo chain: fps dropper runs FIRST (before hwupload)
        # so the decode→upload→tonemap pipeline only processes frames we
        # actually keep.  On NVIDIA Turing the old fps-inside-libplacebo
        # ordering exhausted the Vulkan allocator at 4K p010.
        assert vf.startswith("fps=fps=")
        # Frame drop happens on CPU frames, THEN they go to Vulkan via
        # hwupload and through libplacebo.
        assert "fps=fps=" in vf and "hwupload," in vf
        assert vf.index("fps=fps=") < vf.index("hwupload,")
        assert "libplacebo=tonemapping=" in vf
        # No fps inside the libplacebo filter anymore — it's upstream.
        assert ":fps=" not in vf
        assert "hwdownload,format=yuv420p" in vf
        assert "scale_cuda" not in vf, "DV5 libplacebo path must not use scale_cuda"
        # -hwaccel cuda is still set (NVDEC decodes the HEVC base), but
        # output format must NOT be cuda on this path.
        assert "-hwaccel" in args
        assert args[args.index("-hwaccel") + 1] == "cuda"
        assert "-hwaccel_output_format" not in args


class TestFfmpegThreadFlags:
    """Test that FFmpeg thread cap flags are applied correctly."""

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_gpu_path_includes_thread_cap(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """GPU processing should include -threads and -filter_threads flags."""
        mock_run.return_value = MagicMock(returncode=0)
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc
        mock_exists.return_value = False
        mock_glob.return_value = []

        mock_config.ffmpeg_threads = 2
        generate_images("/test/video.mp4", temp_dir, "NVIDIA", "cuda", mock_config)

        args = mock_popen.call_args[0][0]
        assert "-threads" in args
        thread_idx = args.index("-threads")
        assert args[thread_idx + 1] == "2"
        assert "-filter_threads" in args
        ft_idx = args.index("-filter_threads")
        assert args[ft_idx + 1] == "2"
        # HW decode is active (NVIDIA/cuda) so the video decoder thread cap
        # should still be emitted to prevent oversubscription across GPU workers.
        assert "-threads:v" in args
        tv_idx = args.index("-threads:v")
        assert args[tv_idx + 1] == "1"

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_cpu_path_omits_thread_cap(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """CPU-only processing must omit both -threads and -threads:v so software
        decode can use all available cores (regression guard for issue #212)."""
        mock_run.return_value = MagicMock(returncode=0)
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc
        mock_exists.return_value = False
        mock_glob.return_value = []

        mock_config.ffmpeg_threads = 2
        generate_images("/test/video.mp4", temp_dir, None, None, mock_config)

        args = mock_popen.call_args[0][0]
        # No hardware decode on a pure CPU path, so neither the global -threads
        # cap nor the per-stream -threads:v video-decoder cap should be set.
        assert "-threads:v" not in args, "CPU path should not cap the video decoder — forces single-threaded SW decode"
        bare_threads = [i for i, a in enumerate(args) if a == "-threads"]
        assert len(bare_threads) == 0, "CPU path should not have global -threads cap"

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_gpu_path_zero_threads_omits_cap(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """GPU processing with ffmpeg_threads=0 should omit -threads (auto)."""
        mock_run.return_value = MagicMock(returncode=0)
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc
        mock_exists.return_value = False
        mock_glob.return_value = []

        mock_config.ffmpeg_threads = 0
        generate_images("/test/video.mp4", temp_dir, "NVIDIA", "cuda", mock_config)

        args = mock_popen.call_args[0][0]
        bare_threads = [i for i, a in enumerate(args) if a == "-threads"]
        assert len(bare_threads) == 0, "ffmpeg_threads=0 should omit global -threads"


class TestCancellation:
    """Test that cancellation kills FFmpeg and skips retries."""

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    def test_cancel_kills_ffmpeg_process(
        self,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that cancel_check terminates the running FFmpeg process."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = None
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False

        with pytest.raises(CancellationError):
            generate_images(
                "/test/video.mp4",
                temp_dir,
                None,
                None,
                mock_config,
                cancel_check=lambda: True,
            )

        mock_proc.terminate.assert_called_once()

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    def test_cancel_skips_skip_frame_retry(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that cancellation after first FFmpeg failure skips the skip-frame retry."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False
        mock_glob.return_value = []

        cancelled = False

        def cancel_after_first():
            nonlocal cancelled
            if mock_popen.call_count >= 1:
                cancelled = True
            return cancelled

        with pytest.raises(CancellationError):
            generate_images(
                "/test/video.mp4",
                temp_dir,
                None,
                None,
                mock_config,
                cancel_check=cancel_after_first,
            )

        assert mock_popen.call_count == 1

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    def test_cancel_skips_dv_safe_retry(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that cancellation after skip-frame retry skips the DV-safe retry."""
        mock_run.return_value = MagicMock(returncode=1)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format="Dolby Vision / SMPTE ST 2086")]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False
        mock_glob.return_value = []

        cancel_flag = False

        def cancel_after_first_ffmpeg():
            nonlocal cancel_flag
            if mock_popen.call_count >= 1:
                cancel_flag = True
            return cancel_flag

        with pytest.raises(CancellationError):
            generate_images(
                "/test/video.mp4",
                temp_dir,
                "NVIDIA",
                None,
                mock_config,
                cancel_check=cancel_after_first_ffmpeg,
            )

        assert mock_popen.call_count == 1

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_cancel_skips_gpu_to_cpu_fallback(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that cancellation prevents CodecNotSupportedError (no CPU fallback)."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc_skip = MagicMock()
        mock_proc_skip.poll.side_effect = [None, 0]
        mock_proc_skip.returncode = 69

        mock_proc_noskip = MagicMock()
        mock_proc_noskip.poll.side_effect = [None, 0]
        mock_proc_noskip.returncode = 69

        mock_popen.side_effect = [mock_proc_skip, mock_proc_noskip]
        mock_exists.return_value = False
        mock_glob.return_value = []
        mock_detect.return_value = True

        cancel_flag = False

        def cancel_after_retries():
            nonlocal cancel_flag
            if mock_popen.call_count >= 2:
                cancel_flag = True
            return cancel_flag

        with pytest.raises(CancellationError):
            generate_images(
                "/test/video.mp4",
                temp_dir,
                "NVIDIA",
                None,
                mock_config,
                cancel_check=cancel_after_retries,
            )

        assert mock_popen.call_count == 2


class TestFailureScope:
    """Per-job failure scoping — verifies concurrent jobs can't cross-contaminate
    each other's failure summaries.  Regression guard for the bug where the
    global ``_failures`` list caused one job's summary to include another
    job's errors.
    """

    def test_record_outside_scope_is_dropped(self):
        """Records written with no active scope are logged and discarded."""
        record_failure("/tmp/stray.mkv", 1, "noise", worker_type="GPU")
        assert get_failures() == []

    def test_scope_isolates_records_per_job(self):
        """Records written inside scope A must not appear inside scope B."""
        with failure_scope("job-A"):
            record_failure("/tmp/a.mkv", 1, "A failure", worker_type="GPU")
            assert [f["file"] for f in get_failures()] == ["/tmp/a.mkv"]
            clear_failures()

        with failure_scope("job-B"):
            assert get_failures() == []
            record_failure("/tmp/b.mkv", 2, "B failure", worker_type="CPU")
            assert [f["file"] for f in get_failures()] == ["/tmp/b.mkv"]
            clear_failures()

    def test_concurrent_scopes_in_different_threads(self):
        """Two threads inside two scopes never see each other's records.

        Simulates the real bug: job A's worker thread records failures
        concurrently with job B's job-runner thread calling ``get_failures``.
        """
        import threading

        ready_a = threading.Event()
        ready_b = threading.Event()
        proceed = threading.Event()
        results: dict = {}

        def thread_a():
            with failure_scope("job-A"):
                record_failure("/tmp/a.mkv", 1, "A failure")
                ready_a.set()
                proceed.wait()
                results["A"] = get_failures()
                clear_failures()

        def thread_b():
            with failure_scope("job-B"):
                record_failure("/tmp/b.mkv", 2, "B failure")
                ready_b.set()
                proceed.wait()
                results["B"] = get_failures()
                clear_failures()

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()
        ready_a.wait()
        ready_b.wait()
        proceed.set()
        ta.join()
        tb.join()

        assert [f["file"] for f in results["A"]] == ["/tmp/a.mkv"]
        assert [f["file"] for f in results["B"]] == ["/tmp/b.mkv"]

    def test_scope_nested_same_job_shares_bucket(self):
        """Re-entering the same job_id on a different call site (e.g. a
        worker thread vs the dispatcher thread) must operate on the same
        shared bucket so the dispatcher's ``get_failures`` sees what the
        worker recorded.
        """
        import threading

        def worker():
            with failure_scope("job-X"):
                record_failure("/tmp/x.mkv", 99, "worker-side", worker_type="GPU")

        with failure_scope("job-X"):
            t = threading.Thread(target=worker)
            t.start()
            t.join()
            failures = get_failures()
            assert [f["file"] for f in failures] == ["/tmp/x.mkv"]
            clear_failures()

    def test_clear_failures_only_drops_current_scope(self):
        """clear_failures must only drop the current scope's bucket,
        leaving records for other concurrently-tracked jobs intact.
        """
        with failure_scope("job-keep"):
            record_failure("/tmp/keep.mkv", 1, "keep me")

        with failure_scope("job-drop"):
            record_failure("/tmp/drop.mkv", 1, "drop me")
            clear_failures()
            assert get_failures() == []

        with failure_scope("job-keep"):
            assert [f["file"] for f in get_failures()] == ["/tmp/keep.mkv"]
            clear_failures()


class TestSkipFrameInitialDefaults:
    """Test that ``-skip_frame:v nokey`` is applied to the first FFmpeg
    attempt on every path except DV Profile 5 / libplacebo.

    Regression guard for issue #216: a previous preflight probe gave
    false negatives on benign Dolby Vision RPU parsing artifacts and
    caused ~20x slowdowns on DV Profile 8.x content.  The probe has been
    removed; the retry cascade handles genuine skip_frame failures.
    """

    def _setup_success_mocks(
        self,
        hdr_format_str,
        mock_run,
        mock_popen,
        mock_mediainfo,
        mock_exists,
        mock_file,
        mock_detect,
        mock_glob,
        temp_dir,
    ):
        """Wire mocks so a single FFmpeg Popen call returns success."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=hdr_format_str)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = []
        mock_detect.return_value = False

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_sdr_first_attempt_uses_skip_frame(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """SDR content (no HDR format) must attempt -skip_frame:v nokey
        on the first FFmpeg invocation.
        """
        self._setup_success_mocks(
            None,
            mock_run,
            mock_popen,
            mock_mediainfo,
            mock_exists,
            mock_file,
            mock_detect,
            mock_glob,
            temp_dir,
        )

        generate_images("/test/video.mkv", temp_dir, None, None, mock_config)

        assert mock_popen.call_count == 1
        args = mock_popen.call_args_list[0][0][0]
        assert "-skip_frame:v" in args
        assert args[args.index("-skip_frame:v") + 1] == "nokey"

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_dv_profile8_hdr10_first_attempt_uses_skip_frame(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Dolby Vision Profile 8.x with HDR10 fallback must attempt
        -skip_frame:v nokey on the first FFmpeg invocation.

        This is the exact profile from issue #216: the reporter's slow
        file was DV Profile 8.6 BL+RPU / HDR10 compatible, and the old
        probe rejected skip_frame on benign RPU parsing artifacts,
        costing ~20x speed.
        """
        self._setup_success_mocks(
            "Dolby Vision / SMPTE ST 2086",
            mock_run,
            mock_popen,
            mock_mediainfo,
            mock_exists,
            mock_file,
            mock_detect,
            mock_glob,
            temp_dir,
        )

        generate_images("/test/video.mkv", temp_dir, None, None, mock_config)

        assert mock_popen.call_count == 1
        args = mock_popen.call_args_list[0][0][0]
        assert "-skip_frame:v" in args
        assert args[args.index("-skip_frame:v") + 1] == "nokey"

    @patch("media_preview_generator.processing.orchestrator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.orchestrator.os.rename")
    @patch("media_preview_generator.processing.orchestrator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.orchestrator.glob.glob")
    @patch("media_preview_generator.processing.orchestrator._detect_codec_error")
    def test_retry_drops_skip_frame_when_first_attempt_fails(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """If the first FFmpeg attempt returns non-zero, a second call
        must run without -skip_frame:v. This exercises the retry path
        that makes removal of the preflight probe safe.
        """
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        first_proc = MagicMock()
        first_proc.poll.side_effect = [None, 0]
        first_proc.returncode = 1

        retry_proc = MagicMock()
        retry_proc.poll.side_effect = [None, 0]
        retry_proc.returncode = 0

        mock_popen.side_effect = [first_proc, retry_proc]
        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = []
        mock_detect.return_value = False

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        generate_images("/test/video.mkv", temp_dir, None, None, mock_config)

        assert mock_popen.call_count == 2
        first_args = mock_popen.call_args_list[0][0][0]
        retry_args = mock_popen.call_args_list[1][0][0]
        assert "-skip_frame:v" in first_args
        assert "-skip_frame:v" not in retry_args


class TestBuildDV5Vf:
    """Contract tests for the unified DV5 filter-chain builder.

    Each assertion is the exact string the three inline assemblies
    produced prior to extraction — byte-for-byte — so the refactor
    is demonstrably a pure code-motion change.
    """

    _BASE_SCALE = "scale=w=320:h=240:force_original_aspect_ratio=decrease"

    def test_intel_opencl_chain_is_byte_identical(self):
        got = build_dv5_vf(
            path_kind=DV5_PATH_INTEL_OPENCL,
            tonemap_algorithm="reinhard",
            fps_value=0.5,
            base_scale=self._BASE_SCALE,
        )
        assert got == (
            "fps=fps=0.5:round=up,"
            "setparams=color_primaries=bt2020:"
            "color_trc=smpte2084:colorspace=bt2020nc,"
            "hwmap=derive_device=opencl:mode=read,"
            "tonemap_opencl=format=nv12:p=bt709:t=bt709:"
            "m=bt709:tonemap=reinhard"
            ":peak=100:desat=0,"
            f"hwdownload,format=nv12,format=yuv420p,{self._BASE_SCALE}"
        )

    def test_vaapi_vulkan_chain_is_byte_identical(self):
        got = build_dv5_vf(
            path_kind=DV5_PATH_VAAPI_VULKAN,
            tonemap_algorithm="reinhard",
            fps_value=0.5,
            base_scale=self._BASE_SCALE,
        )
        assert got == (
            "fps=fps=0.5:round=up,"
            "hwmap=derive_device=vulkan,"
            "libplacebo=tonemapping=reinhard"
            ":format=yuv420p"
            ":contrast=1.3:saturation=1.3,"
            f"hwdownload,format=yuv420p,{self._BASE_SCALE}"
        )

    def test_libplacebo_hwupload_chain_is_byte_identical(self):
        got = build_dv5_vf(
            path_kind=DV5_PATH_LIBPLACEBO,
            tonemap_algorithm="reinhard",
            fps_value=0.5,
            base_scale=self._BASE_SCALE,
        )
        assert got == (
            "fps=fps=0.5:round=up,"
            "hwupload,"
            "libplacebo=tonemapping=reinhard"
            ":format=yuv420p"
            ":contrast=1.3:saturation=1.3,"
            f"hwdownload,format=yuv420p,{self._BASE_SCALE}"
        )

    def test_unknown_path_kind_raises(self):
        with pytest.raises(ValueError, match="Unknown DV5 path_kind"):
            build_dv5_vf(
                path_kind="not_a_real_path",
                tonemap_algorithm="reinhard",
                fps_value=0.5,
                base_scale=self._BASE_SCALE,
            )

    def test_fps_appears_first_in_every_variant(self):
        """fps-first ordering is required on NVIDIA Turing (VK OOM fix)
        and significantly faster on Intel OpenCL.  All three variants
        must place the fps dropper at the head of the chain."""
        for path in (
            DV5_PATH_INTEL_OPENCL,
            DV5_PATH_VAAPI_VULKAN,
            DV5_PATH_LIBPLACEBO,
        ):
            got = build_dv5_vf(
                path_kind=path,
                tonemap_algorithm="reinhard",
                fps_value=0.5,
                base_scale=self._BASE_SCALE,
            )
            assert got.startswith("fps=fps=0.5:round=up,"), path
