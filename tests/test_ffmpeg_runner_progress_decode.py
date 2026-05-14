"""Regression: FFmpeg progress-log reader must tolerate non-UTF-8 bytes.

Live failure (2026-05-14, jobs ``a90c9b87`` and earlier): two TV-show files
(Brain Games S01E02, Scrubs S02E18) crashed the runner with
``UnicodeDecodeError: 'utf-8' codec can't decode byte 0xc5 in position 1989:
invalid continuation byte``.

The traceback ended at ``processing/ffmpeg_runner.py:534`` —
``with open(output_file, encoding="utf-8") as f: lines = f.readlines()``.
FFmpeg can emit non-UTF-8 bytes in stderr (Latin-1 metadata in stream tags,
non-ASCII paths, etc.), and a strict UTF-8 decode crashes the runner mid-loop.
The user-visible error then mislabels it as "corrupt video file" because the
catch-all in ``multi_server.process_canonical_path`` wraps every exception
that bubbles out of ``generate_images``.

This test invokes ``create_ffmpeg_runner`` with a mocked ``subprocess.Popen``
that writes non-UTF-8 bytes to the runner's progress-log file before
"exiting". Pre-fix this raised UnicodeDecodeError; post-fix the bytes are
decoded with ``errors="replace"`` and the runner returns cleanly.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.processing.ffmpeg_runner import create_ffmpeg_runner


def _runner_kwargs(tmp_path):
    """Minimal kwargs to instantiate the runner — only the bits the SUT touches."""
    config = SimpleNamespace(
        ffmpeg_path="/usr/bin/ffmpeg",
        plex_bif_frame_interval=10,
        thumbnail_quality=4,
        log_level="INFO",
        ffmpeg_threads=1,
    )
    return dict(
        video_file="/fake/source.mkv",
        output_folder=str(tmp_path),
        gpu=None,
        gpu_device_path=None,
        config=config,
        progress_callback=None,
        ffmpeg_threads_override=None,
        cancel_check=None,
        pause_check=None,
        path_kind="sdr",
        libplacebo_vf=None,
        use_libplacebo=False,
        dv5_software_fallback=False,
        base_scale="scale=w=320:h=240:force_original_aspect_ratio=decrease",
        fps_filter="fps=fps=0.1:round=up",
        hdr10_zscale_chain="",
    )


def test_runner_survives_non_utf8_progress_bytes(tmp_path):
    """FFmpeg writes a Latin-1 byte (0xc5) to stderr; the runner must not crash.

    Pre-fix: ``f.readlines()`` raised ``UnicodeDecodeError`` and the entire
    item was reported to the user as a "corrupt video file" — even though
    FFmpeg itself was running fine. Post-fix: bytes survive via
    ``errors="replace"`` and the runner reports the FFmpeg exit code normally.
    """
    runner_kwargs = _runner_kwargs(tmp_path)

    # Captured by the Popen mock so we can write garbage bytes to the
    # path the runner is polling.
    captured_output_file: list[str] = []

    real_open = open

    def open_spy(path, *args, **kwargs):
        # Detect the runner's stderr-log open: text-mode "w" on a path
        # under tempdir whose basename starts with "ffmpeg_output_".
        if (
            isinstance(path, str)
            and os.path.basename(path).startswith("ffmpeg_output_")
            and (args == ("w",) or kwargs.get("mode") == "w")
        ):
            captured_output_file.append(path)
        return real_open(path, *args, **kwargs)

    def popen_side_effect(*_a, **_kw):
        # Wait until the runner has opened its stderr-log path, then
        # write raw non-UTF-8 bytes there. The runner's polling loop
        # will read the file before checking poll() again.
        for _ in range(200):
            if captured_output_file:
                with real_open(captured_output_file[0], "wb") as fh:
                    # 1989 ASCII bytes then a Latin-1 0xc5 byte —
                    # mirrors the live UnicodeDecodeError byte position.
                    fh.write(b"x" * 1989 + b"\xc5\n")
                break
            time.sleep(0.001)
        proc = MagicMock()
        # First poll() returns None (still running, triggers the read),
        # subsequent polls return 0 (clean exit).
        proc.poll.side_effect = [None, 0, 0, 0]
        proc.returncode = 0
        proc.pid = 12345
        return proc

    with patch("builtins.open", side_effect=open_spy), patch("subprocess.Popen", side_effect=popen_side_effect):
        runner = create_ffmpeg_runner(**runner_kwargs)
        # No exception = bug fixed. Pre-fix this raised UnicodeDecodeError
        # from inside the readlines() call.
        try:
            rc, _seconds, _speed, stderr_lines = runner(use_skip=False, init_vulkan=False)
        except UnicodeDecodeError as exc:  # pragma: no cover - regression guard
            pytest.fail(
                f"runner raised UnicodeDecodeError on non-UTF-8 progress bytes: {exc}. "
                f"This is the Brain Games / Scrubs regression — the open() at the "
                f"two read sites in ffmpeg_runner.py must use errors='replace'."
            )

    # Sanity: returncode round-tripped and the non-UTF-8 byte was decoded
    # (replacement char or any preserved-with-replace form is fine — the
    # contract is "don't crash", not "preserve exact bytes").
    assert rc == 0
    # The 0xc5 line should have surfaced (possibly with U+FFFD), proving
    # the read path actually executed against the bytes.
    assert any("x" * 1000 in line for line in stderr_lines)
