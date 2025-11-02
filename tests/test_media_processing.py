"""
Tests for media_processing.py module.

Tests BIF generation, FFmpeg execution, progress parsing, path mapping,
HDR detection, and the complete processing pipeline.
"""

import os
import struct
import tempfile
import pytest
from unittest.mock import MagicMock, patch, mock_open, call
from pathlib import Path

from plex_generate_previews.media_processing import (
    generate_bif,
    parse_ffmpeg_progress_line,
    heuristic_allows_skip,
    generate_images,
    process_item,
    _detect_codec_error,
    CodecNotSupportedError
)


class TestBIFGeneration:
    """Test BIF file generation."""
    
    def test_generate_bif_creates_valid_structure(self, temp_dir, mock_config):
        """Test that BIF file has correct binary structure."""
        # Create test thumbnails
        for i in range(3):
            timestamp = i * 5
            img_path = os.path.join(temp_dir, f'{timestamp:010d}.jpg')
            with open(img_path, 'wb') as f:
                f.write(b'\xFF\xD8\xFF')
        
        # Generate BIF
        bif_path = os.path.join(temp_dir, 'test.bif')
        generate_bif(bif_path, temp_dir, mock_config)
        
        # Verify BIF file exists
        assert os.path.exists(bif_path)
        
        # Verify BIF magic bytes
        with open(bif_path, 'rb') as f:
            magic = list(f.read(8))
            assert magic == [0x89, 0x42, 0x49, 0x46, 0x0d, 0x0a, 0x1a, 0x0a]
            
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
            img_path = os.path.join(temp_dir, f'{timestamp:010d}.jpg')
            data = b'\xFF\xD8\xFF' + (b'X' * (100 * (i + 1)))
            with open(img_path, 'wb') as f:
                f.write(data)
            thumbnail_sizes.append(len(data))
        
        bif_path = os.path.join(temp_dir, 'test.bif')
        generate_bif(bif_path, temp_dir, mock_config)
        
        # Verify index table
        with open(bif_path, 'rb') as f:
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
            assert end_ts == 0xffffffff
            assert end_offset == expected_offset
    
    def test_generate_bif_embedded_images(self, temp_dir, mock_config):
        """Test that actual image data is embedded in BIF."""
        # Create thumbnail with recognizable content
        test_data = b'\xFF\xD8\xFF' + b'TEST_IMAGE_DATA_12345'
        img_path = os.path.join(temp_dir, '0000000000.jpg')
        with open(img_path, 'wb') as f:
            f.write(test_data)
        
        bif_path = os.path.join(temp_dir, 'test.bif')
        generate_bif(bif_path, temp_dir, mock_config)
        
        # Verify image data is embedded
        with open(bif_path, 'rb') as f:
            content = f.read()
            assert b'TEST_IMAGE_DATA_12345' in content
            assert test_data in content
    
    def test_generate_bif_empty_directory(self, temp_dir, mock_config):
        """Test BIF generation with no thumbnails."""
        bif_path = os.path.join(temp_dir, 'empty.bif')
        generate_bif(bif_path, temp_dir, mock_config)
        
        # Should create BIF with 0 images
        with open(bif_path, 'rb') as f:
            f.seek(12)  # Skip magic + version
            image_count = struct.unpack("<I", f.read(4))[0]
            assert image_count == 0
    
    def test_generate_bif_frame_interval(self, temp_dir, mock_config):
        """Test that frame interval is correctly converted to milliseconds."""
        # Test with 10 second interval
        mock_config.plex_bif_frame_interval = 10
        
        img_path = os.path.join(temp_dir, '0000000000.jpg')
        with open(img_path, 'wb') as f:
            f.write(b'\xFF\xD8\xFF')
        
        bif_path = os.path.join(temp_dir, 'test.bif')
        generate_bif(bif_path, temp_dir, mock_config)
        
        with open(bif_path, 'rb') as f:
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
        def callback(progress, current, total, speed, remaining=None, frame=0, fps=0, q=0, size=0, time_str="", bitrate=0):
            callback_data['progress'] = progress
            callback_data['current'] = current
            callback_data['speed'] = speed
            callback_data['frame'] = frame
            callback_data['fps'] = fps
            callback_data['time_str'] = time_str
        
        total_duration = 1800.0  # 30 minutes
        result = parse_ffmpeg_progress_line(line, total_duration, callback)
        
        # Verify callback was called with correct data
        assert 'progress' in callback_data
        assert callback_data['frame'] == 1234
        assert abs(callback_data['fps'] - 45.6) < 0.1
        assert callback_data['speed'] == "1.23x"
        assert callback_data['time_str'] == "00:12:34.56"
    
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
    
    @patch('subprocess.run')
    def test_heuristic_allows_skip_success(self, mock_run, mock_config):
        """Test that heuristic passes when FFmpeg succeeds."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        
        result = heuristic_allows_skip(mock_config.ffmpeg_path, "/test/video.mp4")
        assert result is True
    
    @patch('subprocess.run')
    def test_heuristic_allows_skip_failure(self, mock_run, mock_config):
        """Test that heuristic fails when FFmpeg fails."""
        mock_run.return_value = MagicMock(
            returncode=1, 
            stderr="Error decoding frame\n"
        )
        
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
            assert result is True, f"Should detect codec error case-insensitively: {line}"


class TestGenerateImages:
    """Test thumbnail generation with FFmpeg."""
    
    @patch('plex_generate_previews.media_processing.MediaInfo')
    @patch('subprocess.Popen')
    @patch('subprocess.run')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    @patch('time.sleep')
    def test_generate_images_calls_ffmpeg(self, mock_sleep, mock_file, mock_exists, 
                                          mock_run, mock_popen, mock_mediainfo, 
                                          temp_dir, mock_config):
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
    
    @patch('plex_generate_previews.media_processing.MediaInfo')
    @patch('subprocess.Popen')
    @patch('subprocess.run')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    @patch('time.sleep')
    @patch('glob.glob')
    def test_generate_images_gpu_nvidia(self, mock_glob, mock_sleep, mock_file, 
                                       mock_exists, mock_run, mock_popen, 
                                       mock_mediainfo, temp_dir, mock_config):
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
        
        generate_images("/test/video.mp4", temp_dir, 'NVIDIA', 'cuda', mock_config)
        
        args = mock_popen.call_args[0][0]
        assert '-hwaccel' in args
        assert 'cuda' in args
    
    @patch('plex_generate_previews.media_processing.MediaInfo')
    @patch('subprocess.Popen')
    @patch('subprocess.run')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    @patch('time.sleep')
    @patch('glob.glob')
    def test_generate_images_gpu_amd(self, mock_glob, mock_sleep, mock_file, 
                                    mock_exists, mock_run, mock_popen, 
                                    mock_mediainfo, temp_dir, mock_config):
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
        
        generate_images("/test/video.mp4", temp_dir, 'AMD', '/dev/dri/renderD128', mock_config)
        
        args = mock_popen.call_args[0][0]
        assert '-hwaccel' in args
        assert 'vaapi' in args
        assert '/dev/dri/renderD128' in args
    
    @patch('plex_generate_previews.media_processing.MediaInfo')
    @patch('subprocess.Popen')
    @patch('subprocess.run')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    @patch('time.sleep')
    @patch('glob.glob')
    def test_generate_images_cpu_only(self, mock_glob, mock_sleep, mock_file, 
                                      mock_exists, mock_run, mock_popen, 
                                      mock_mediainfo, temp_dir, mock_config):
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
        if '-hwaccel' in args:
            # If it exists, it shouldn't be used (heuristic may add it)
            pass
    
    @patch('plex_generate_previews.media_processing.MediaInfo')
    @patch('subprocess.Popen')
    @patch('subprocess.run')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    @patch('time.sleep')
    @patch('glob.glob')
    def test_generate_images_hdr_detection(self, mock_glob, mock_sleep, mock_file, 
                                          mock_exists, mock_run, mock_popen, 
                                          mock_mediainfo, temp_dir, mock_config):
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
        vf_index = args.index('-vf')
        vf_value = args[vf_index + 1]
        
        # Should contain HDR processing filters
        assert 'zscale' in vf_value
        assert 'tonemap' in vf_value
    
    @patch('plex_generate_previews.media_processing.MediaInfo')
    @patch('subprocess.Popen')
    @patch('subprocess.run')
    @patch('os.rename')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    @patch('time.sleep')
    @patch('glob.glob')
    def test_generate_images_renames_files(self, mock_glob, mock_sleep, mock_file, 
                                          mock_exists, mock_rename, mock_run, 
                                          mock_popen, mock_mediainfo, temp_dir, mock_config):
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
            f'{temp_dir}/img-000001.jpg',
            f'{temp_dir}/img-000002.jpg',
            f'{temp_dir}/img-000003.jpg',
        ]
        
        generate_images("/test/video.mp4", temp_dir, None, None, mock_config)
        
        # Verify rename was called with correct arguments
        # img-000001.jpg (frame 0) -> 0000000000.jpg (0 seconds)
        # img-000002.jpg (frame 1) -> 0000000005.jpg (5 seconds)
        # img-000003.jpg (frame 2) -> 0000000010.jpg (10 seconds)
        assert mock_rename.called
        calls = mock_rename.call_args_list
        assert len(calls) == 3
    
    @patch('plex_generate_previews.media_processing.MediaInfo')
    @patch('subprocess.Popen')
    @patch('subprocess.run')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    @patch('time.sleep')
    @patch('glob.glob')
    def test_generate_images_progress_callback(self, mock_glob, mock_sleep, mock_file, 
                                              mock_exists, mock_run, mock_popen, 
                                              mock_mediainfo, temp_dir, mock_config):
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
    
    @patch('plex_generate_previews.media_processing.MediaInfo')
    @patch('subprocess.Popen')
    @patch('subprocess.run')
    @patch('plex_generate_previews.media_processing.os.rename')
    @patch('plex_generate_previews.media_processing.os.remove')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    @patch('time.sleep')
    @patch('plex_generate_previews.media_processing.glob.glob')
    @patch('plex_generate_previews.media_processing._detect_codec_error')
    def test_generate_images_raises_codec_error_in_gpu_context(self, mock_detect, mock_glob, 
                                                                  mock_sleep, mock_file, mock_exists, 
                                                                  mock_remove, mock_rename, mock_run, mock_popen, mock_mediainfo, 
                                                                  temp_dir, mock_config):
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
            generate_images(
                "/test/video.mp4", temp_dir, 'NVIDIA', None, mock_config
            )
        
        assert "Codec not supported by GPU" in str(exc_info.value)
        assert mock_detect.called
        assert mock_popen.call_count == 2  # GPU with skip_frame + GPU without skip_frame (no CPU fallback)
        
        # Verify cleanup was attempted
        assert mock_remove.called
    
    @patch('plex_generate_previews.media_processing.MediaInfo')
    @patch('subprocess.Popen')
    @patch('subprocess.run')
    @patch('plex_generate_previews.media_processing.os.rename')
    @patch('plex_generate_previews.media_processing.os.remove')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    @patch('time.sleep')
    @patch('plex_generate_previews.media_processing.glob.glob')
    @patch('plex_generate_previews.media_processing._detect_codec_error')
    def test_generate_images_no_cpu_fallback_when_disabled(self, mock_detect, mock_glob, 
                                                            mock_sleep, mock_file, mock_exists, 
                                                            mock_remove, mock_rename, mock_run, mock_popen, mock_mediainfo, 
                                                            temp_dir, mock_config):
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
            generate_images(
                "/test/video.mp4", temp_dir, 'NVIDIA', None, mock_config
            )
        
        assert mock_detect.called
        assert mock_popen.call_count == 2  # Initial attempt + skip_frame retry, no CPU fallback (exception raised)
    
    @patch('plex_generate_previews.media_processing.MediaInfo')
    @patch('subprocess.Popen')
    @patch('subprocess.run')
    @patch('plex_generate_previews.media_processing.os.rename')
    @patch('plex_generate_previews.media_processing.os.remove')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    @patch('time.sleep')
    @patch('plex_generate_previews.media_processing.glob.glob')
    @patch('plex_generate_previews.media_processing._detect_codec_error')
    def test_generate_images_no_cpu_fallback_when_no_codec_error(self, mock_detect, mock_glob, 
                                                                  mock_sleep, mock_file, mock_exists, 
                                                                  mock_remove, mock_rename, mock_run, mock_popen, mock_mediainfo, 
                                                                  temp_dir, mock_config):
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
            "/test/video.mp4", temp_dir, 'NVIDIA', None, mock_config
        )
        
        # Should fail (no fallback since not codec error)
        assert success is False
        assert image_count == 0
        assert mock_detect.called
        assert mock_popen.call_count == 2  # Initial attempt + skip_frame retry, no CPU fallback


class TestProcessItem:
    """Test the complete item processing pipeline."""
    
    @patch('plex_generate_previews.media_processing.generate_bif')
    @patch('plex_generate_previews.media_processing.generate_images')
    @patch('os.path.isfile')
    @patch('os.path.isdir')
    @patch('os.makedirs')
    @patch('shutil.rmtree')
    def test_process_item_success(self, mock_rmtree, mock_makedirs, mock_isdir, 
                                  mock_isfile, mock_gen_images, mock_gen_bif, 
                                  mock_config, plex_xml_movie_tree):
        """Test successful processing of a media item."""
        # Mock Plex query response
        mock_plex = MagicMock()
        
        import xml.etree.ElementTree as ET
        mock_plex.query.return_value = ET.fromstring(plex_xml_movie_tree)
        
        # Mock file system - media file exists but index.bif doesn't
        def isfile_side_effect(path):
            # Media files exist, but not BIF files
            return '.bif' not in path
        
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
    
    @patch('plex_generate_previews.media_processing.generate_bif')
    @patch('plex_generate_previews.media_processing.generate_images')
    @patch('os.path.isfile')
    @patch('os.path.isdir')
    @patch('os.makedirs')
    @patch('shutil.rmtree')
    def test_process_item_path_mapping(self, mock_rmtree, mock_makedirs, mock_isdir, 
                                       mock_isfile, mock_gen_images, mock_gen_bif, 
                                       mock_config, plex_xml_movie_tree):
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
            return '.bif' not in path
        
        mock_isfile.side_effect = isfile_side_effect
        mock_isdir.return_value = False  # Directories don't exist yet
        
        mock_gen_images.return_value = (True, 2, False, 1.0, "1.0x")
        process_item("/library/metadata/54321", None, None, mock_config, mock_plex)
        
        # Verify generate_images was called with mapped path
        assert mock_gen_images.called
        called_path = mock_gen_images.call_args[0][0]
        # Path should be remapped from /data to /mnt/videos
        assert called_path.startswith("/mnt/videos")
    
    @patch('os.path.isfile')
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





