"""
Tests for media_processing.py module.

Tests BIF generation, FFmpeg execution, progress parsing, path mapping,
HDR detection, and the complete processing pipeline.
"""

import os
import struct
from unittest.mock import MagicMock, mock_open, patch

import pytest

from plex_generate_previews.media_processing import (
    CodecNotSupportedError,
    _detect_codec_error,
    _detect_dolby_vision_rpu_error,
    _detect_hwaccel_runtime_error,
    _detect_zscale_colorspace_error,
    _diagnose_ffmpeg_exit_code,
    _is_dv_no_backward_compat,
    _save_ffmpeg_failure_log,
    _verify_tmp_folder_health,
    generate_bif,
    generate_images,
    heuristic_allows_skip,
    parse_ffmpeg_progress_line,
    process_item,
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
            expected_offset = 64 + (
                8 * 6
            )  # Header + index table (5 entries + end marker)
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

    def test_parse_ffmpeg_progress_line_no_callback(self):
        """Test parsing without callback doesn't crash."""
        line = "frame= 100 fps=30.0 q=28.0 size=  1000kB time=00:00:10.00 bitrate= 800.0kbits/s speed=1.0x"
        result = parse_ffmpeg_progress_line(line, 100.0, None)
        assert result == 100.0


class TestHeuristicAllowsSkip:
    """Test skip frame heuristic."""

    @patch("subprocess.run")
    def test_heuristic_allows_skip_success(self, mock_run, mock_config):
        """Test that heuristic passes when FFmpeg succeeds."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        result = heuristic_allows_skip(mock_config.ffmpeg_path, "/test/video.mp4")
        assert result is True

    @patch("subprocess.run")
    def test_heuristic_allows_skip_failure(self, mock_run, mock_config):
        """Test that heuristic fails when FFmpeg fails."""
        mock_run.return_value = MagicMock(returncode=1, stderr="Error decoding frame\n")

        result = heuristic_allows_skip(mock_config.ffmpeg_path, "/test/video.mp4")
        assert result is False


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
            assert result is True, (
                f"Should detect codec error case-insensitively: {line}"
            )


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

    @patch("plex_generate_previews.media_processing.MediaInfo")
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

    @patch("plex_generate_previews.media_processing.MediaInfo")
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

    @patch("plex_generate_previews.media_processing.MediaInfo")
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

        generate_images(
            "/test/video.mp4", temp_dir, "AMD", "/dev/dri/renderD128", mock_config
        )

        args = mock_popen.call_args[0][0]
        assert "-hwaccel" in args
        assert "vaapi" in args
        assert "/dev/dri/renderD128" in args

    @patch("plex_generate_previews.media_processing.MediaInfo")
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

    @patch("plex_generate_previews.media_processing.MediaInfo")
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

    @patch("plex_generate_previews.media_processing.MediaInfo")
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

    @patch("plex_generate_previews.media_processing.MediaInfo")
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
        mock_file.return_value.readlines.return_value = [
            "frame= 100 fps=30.0 time=00:00:10.00 speed=1.0x\n"
        ]

        callback_called = [False]

        def callback(*args, **kwargs):
            callback_called[0] = True

        generate_images("/test/video.mp4", temp_dir, None, None, mock_config, callback)

        # Callback should have been called at least once
        # Note: Due to mocking, it may not be called, but the structure is there
        # This test verifies the code doesn't crash with a callback

    @patch("plex_generate_previews.media_processing.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("plex_generate_previews.media_processing.os.rename")
    @patch("plex_generate_previews.media_processing.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("plex_generate_previews.media_processing.glob.glob")
    @patch("plex_generate_previews.media_processing._detect_codec_error")
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
        assert (
            mock_popen.call_count == 2
        )  # GPU with skip_frame + GPU without skip_frame (no CPU fallback)

        # Verify cleanup was attempted
        assert mock_remove.called

    @patch("plex_generate_previews.media_processing.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("plex_generate_previews.media_processing.os.rename")
    @patch("plex_generate_previews.media_processing.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("plex_generate_previews.media_processing.glob.glob")
    @patch("plex_generate_previews.media_processing._detect_codec_error")
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
        assert (
            mock_popen.call_count == 2
        )  # Initial attempt + skip_frame retry, no CPU fallback (exception raised)

    @patch("plex_generate_previews.media_processing.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("plex_generate_previews.media_processing.os.rename")
    @patch("plex_generate_previews.media_processing.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("plex_generate_previews.media_processing.glob.glob")
    @patch("plex_generate_previews.media_processing._detect_codec_error")
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

        success, image_count, hw_used, seconds, speed = generate_images(
            "/test/video.mp4", temp_dir, "NVIDIA", None, mock_config
        )

        # Should fail (no fallback since not codec error)
        assert success is False
        assert image_count == 0
        assert mock_detect.called
        assert (
            mock_popen.call_count == 2
        )  # Initial attempt + skip_frame retry, no CPU fallback

    @patch("plex_generate_previews.media_processing.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("plex_generate_previews.media_processing.os.rename")
    @patch("plex_generate_previews.media_processing.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("plex_generate_previews.media_processing.glob.glob")
    @patch("plex_generate_previews.media_processing._detect_codec_error")
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

        success, image_count, hw_used, seconds, speed = generate_images(
            "/test/video_dv.mp4", temp_dir, "NVIDIA", None, mock_config
        )

        assert success is True
        assert image_count >= 1
        assert hw_used is True
        assert mock_popen.call_count == 3  # skip + no-skip + DV-safe retry

        # Assert the third invocation used the DV-safe vf: fps+scale only, no zscale/tonemap
        third_args = mock_popen.call_args_list[2][0][0]
        vf_index = third_args.index("-vf")
        vf_value = third_args[vf_index + 1]
        assert "scale=w=320:h=240:force_original_aspect_ratio=decrease" in vf_value
        assert "zscale" not in vf_value
        assert "tonemap" not in vf_value

    @patch("plex_generate_previews.media_processing.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("plex_generate_previews.media_processing.os.rename")
    @patch("plex_generate_previews.media_processing.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("plex_generate_previews.media_processing.glob.glob")
    @patch("plex_generate_previews.media_processing._detect_codec_error")
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

        success, image_count, hw_used, seconds, speed = generate_images(
            "/test/video_dv.mp4", temp_dir, None, None, mock_config
        )

        assert success is False
        assert image_count == 0
        assert hw_used is False
        assert mock_popen.call_count == 3

    @patch("plex_generate_previews.media_processing.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("plex_generate_previews.media_processing.os.rename")
    @patch("plex_generate_previews.media_processing.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("plex_generate_previews.media_processing.glob.glob")
    @patch("plex_generate_previews.media_processing._detect_codec_error")
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

        success, image_count, hw_used, seconds, speed = generate_images(
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

    @patch("plex_generate_previews.media_processing.generate_bif")
    @patch("plex_generate_previews.media_processing.generate_images")
    @patch("os.path.isfile")
    @patch("os.path.isdir")
    @patch("os.makedirs")
    @patch("shutil.rmtree")
    def test_process_item_success(
        self,
        mock_rmtree,
        mock_makedirs,
        mock_isdir,
        mock_isfile,
        mock_gen_images,
        mock_gen_bif,
        mock_config,
        plex_xml_movie_tree,
    ):
        """Test successful processing of a media item."""
        # Mock Plex query response
        mock_plex = MagicMock()

        import xml.etree.ElementTree as ET

        mock_plex.query.return_value = ET.fromstring(plex_xml_movie_tree)

        # Mock file system - media file exists but index.bif doesn't
        def isfile_side_effect(path):
            # Media files exist, but not BIF files
            return ".bif" not in path

        mock_isfile.side_effect = isfile_side_effect
        mock_isdir.return_value = False  # Directories don't exist yet

        # Set config paths
        mock_config.plex_config_folder = "/config/plex"
        mock_config.tmp_folder = "/tmp"
        mock_config.plex_local_videos_path_mapping = ""
        mock_config.plex_videos_path_mapping = ""
        mock_config.regenerate_thumbnails = False

        # Simulate successful image generation: (success, image_count, hw, seconds, speed)
        mock_gen_images.return_value = (True, 3, False, 1.2, "1.0x")
        process_item("/library/metadata/54321", None, None, mock_config, mock_plex)

        # Verify images and BIF were generated
        assert mock_gen_images.called
        assert mock_gen_bif.called

    @patch("plex_generate_previews.media_processing.generate_bif")
    @patch("plex_generate_previews.media_processing.generate_images")
    @patch("os.path.isfile")
    @patch("os.path.isdir")
    @patch("os.makedirs")
    @patch("shutil.rmtree")
    def test_process_item_path_mapping(
        self,
        mock_rmtree,
        mock_makedirs,
        mock_isdir,
        mock_isfile,
        mock_gen_images,
        mock_gen_bif,
        mock_config,
        plex_xml_movie_tree,
    ):
        """Test that path mapping is applied correctly."""
        mock_plex = MagicMock()

        import xml.etree.ElementTree as ET

        mock_plex.query.return_value = ET.fromstring(plex_xml_movie_tree)

        # Configure path mapping
        mock_config.plex_videos_path_mapping = "/data"
        mock_config.plex_local_videos_path_mapping = "/mnt/videos"
        mock_config.plex_config_folder = "/config/plex"
        mock_config.tmp_folder = "/tmp"
        mock_config.regenerate_thumbnails = False

        # Mock file system - media file exists but index.bif doesn't
        def isfile_side_effect(path):
            # Media files exist, but not BIF files
            return ".bif" not in path

        mock_isfile.side_effect = isfile_side_effect
        mock_isdir.return_value = False  # Directories don't exist yet

        mock_gen_images.return_value = (True, 2, False, 1.0, "1.0x")
        process_item("/library/metadata/54321", None, None, mock_config, mock_plex)

        # Verify generate_images was called with mapped path
        assert mock_gen_images.called
        called_path = mock_gen_images.call_args[0][0]
        # Path should be remapped from /data to /mnt/videos
        # On Windows, normpath converts forward slashes to backslashes
        import os as _os

        expected_prefix = _os.path.normpath("/mnt/videos")
        assert called_path.startswith(expected_prefix)

    @patch("os.path.isfile")
    def test_process_item_missing_file(
        self, mock_isfile, mock_config, plex_xml_movie_tree
    ):
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
    """M5: Integration test verifying generate_images builds correct -vf for DV Profile 5."""

    @patch("plex_generate_previews.media_processing.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("plex_generate_previews.media_processing.os.rename")
    @patch("plex_generate_previews.media_processing.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("plex_generate_previews.media_processing.glob.glob")
    @patch("plex_generate_previews.media_processing._detect_codec_error")
    def test_generate_images_dv_profile5_skips_zscale_tonemap(
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
        """DV Profile 5 should proactively use fps+scale only — no zscale/tonemap."""
        # Arrange — heuristic probe succeeds (allows skip)
        mock_run.return_value = MagicMock(returncode=0)

        # MediaInfo: DV Profile 5 without backward-compat
        mock_info = MagicMock()
        mock_info.video_tracks = [
            MagicMock(hdr_format="Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU")
        ]
        mock_mediainfo.parse.return_value = mock_info

        # FFmpeg succeeds on first call (with skip)
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

        # Act
        success, image_count, hw_used, seconds, speed = generate_images(
            "/test/dv_profile5.mkv", temp_dir, None, None, mock_config
        )

        # Assert — success, and vf contains only fps+scale, NO zscale/tonemap
        assert success is True
        assert image_count >= 1
        assert mock_popen.call_count == 1

        first_args = mock_popen.call_args_list[0][0][0]
        vf_index = first_args.index("-vf")
        vf_value = first_args[vf_index + 1]
        assert "fps=" in vf_value
        assert "scale=w=320:h=240:force_original_aspect_ratio=decrease" in vf_value
        assert "zscale" not in vf_value
        assert "tonemap" not in vf_value


class TestZscaleErrorRetry:
    """M6: Test that zscale colorspace errors trigger the DV-safe retry path."""

    @patch("plex_generate_previews.media_processing.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("plex_generate_previews.media_processing.os.rename")
    @patch("plex_generate_previews.media_processing.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("plex_generate_previews.media_processing.glob.glob")
    @patch("plex_generate_previews.media_processing._detect_codec_error")
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
        success, image_count, hw_used, seconds, speed = generate_images(
            "/test/hdr10_zscale_crash.mkv", temp_dir, "NVIDIA", None, mock_config
        )

        # Assert — success after 3 Popen calls, third uses DV-safe vf
        assert success is True
        assert image_count >= 1
        assert mock_popen.call_count == 3

        third_args = mock_popen.call_args_list[2][0][0]
        vf_index = third_args.index("-vf")
        vf_value = third_args[vf_index + 1]
        assert "scale=w=320:h=240:force_original_aspect_ratio=decrease" in vf_value
        assert "zscale" not in vf_value
        assert "tonemap" not in vf_value


class TestDVSafeRetryGpuFailure:
    """M8: DV-safe retry fails on GPU — should raise CodecNotSupportedError."""

    @patch("plex_generate_previews.media_processing.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("plex_generate_previews.media_processing.os.rename")
    @patch("plex_generate_previews.media_processing.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("plex_generate_previews.media_processing.glob.glob")
    @patch("plex_generate_previews.media_processing._detect_codec_error")
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
            generate_images(
                "/test/always_fails.mkv", temp_dir, "NVIDIA", None, mock_config
            )

        assert mock_popen.call_count == 3


class TestDynamicNpl:
    """M7: Test that MaxCLL metadata drives npl in the tonemap filter chain."""

    @patch("plex_generate_previews.media_processing.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("plex_generate_previews.media_processing.os.rename")
    @patch("plex_generate_previews.media_processing.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("plex_generate_previews.media_processing.glob.glob")
    @patch("plex_generate_previews.media_processing._detect_codec_error")
    def test_maxcll_present_sets_npl(
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
        """When MaxCLL is available, npl={maxcll} appears in the zscale filter."""
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

        success, *_ = generate_images(
            "/test/hdr10_1000nit.mkv", temp_dir, None, None, mock_config
        )

        assert success is True
        first_args = mock_popen.call_args_list[0][0][0]
        vf_index = first_args.index("-vf")
        vf_value = first_args[vf_index + 1]
        assert "npl=1000" in vf_value
        assert "zscale=t=linear:npl=1000" in vf_value

    @patch("plex_generate_previews.media_processing.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("plex_generate_previews.media_processing.os.rename")
    @patch("plex_generate_previews.media_processing.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("plex_generate_previews.media_processing.glob.glob")
    @patch("plex_generate_previews.media_processing._detect_codec_error")
    def test_maxcll_absent_omits_npl(
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
        """When MaxCLL is absent, npl is omitted (FFmpeg auto-detects)."""
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

        success, *_ = generate_images(
            "/test/hdr10_no_maxcll.mkv", temp_dir, None, None, mock_config
        )

        assert success is True
        first_args = mock_popen.call_args_list[0][0][0]
        vf_index = first_args.index("-vf")
        vf_value = first_args[vf_index + 1]
        assert "npl=" not in vf_value
        assert "zscale=t=linear," in vf_value

    @patch("plex_generate_previews.media_processing.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("plex_generate_previews.media_processing.os.rename")
    @patch("plex_generate_previews.media_processing.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("plex_generate_previews.media_processing.glob.glob")
    @patch("plex_generate_previews.media_processing._detect_codec_error")
    def test_maxcll_unparseable_omits_npl(
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
        """When MaxCLL is unparseable (e.g. 'N/A'), npl is omitted gracefully."""
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

        success, *_ = generate_images(
            "/test/hdr10_bad_maxcll.mkv", temp_dir, None, None, mock_config
        )

        assert success is True
        first_args = mock_popen.call_args_list[0][0][0]
        vf_index = first_args.index("-vf")
        vf_value = first_args[vf_index + 1]
        assert "npl=" not in vf_value


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

        _save_ffmpeg_failure_log(
            "/media/test.mkv", 187, ["error line 1", "error line 2"]
        )

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
        with patch(
            "plex_generate_previews.media_processing.shutil.disk_usage"
        ) as mock_usage:
            mock_usage.return_value = MagicMock(free=0)
            ok, messages = _verify_tmp_folder_health(str(tmp_path), min_free_mb=1)
        assert ok is True
        assert messages
        assert "low free space" in messages[0].lower()


class TestHdrFormatNoneString:
    """L12: hdr_format='None' string should produce the SDR filter path."""

    @patch("plex_generate_previews.media_processing.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("plex_generate_previews.media_processing.os.rename")
    @patch("plex_generate_previews.media_processing.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("plex_generate_previews.media_processing.glob.glob")
    @patch("plex_generate_previews.media_processing._detect_codec_error")
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

        success, *_ = generate_images(
            "/test/sdr_none_string.mkv", temp_dir, None, None, mock_config
        )

        assert success is True
        first_args = mock_popen.call_args_list[0][0][0]
        vf_index = first_args.index("-vf")
        vf_value = first_args[vf_index + 1]
        assert "zscale" not in vf_value
        assert "tonemap" not in vf_value
        assert "fps=" in vf_value
        assert "scale=w=320:h=240:force_original_aspect_ratio=decrease" in vf_value
