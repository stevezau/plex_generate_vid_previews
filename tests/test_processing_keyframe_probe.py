"""Tests for the keyframe-probe and duplicate-thumbnail helpers added
to fix issue #238 (duplicate frames in BIF when ``-skip_frame:v nokey``
runs against a file whose keyframe spacing is larger than the user's
thumbnail interval).

The unit tests cover the helpers in isolation (ffprobe-parser + adjacent-
hash counter).  A separate integration block at the bottom exercises the
wiring inside ``generate_images`` itself, asserting that the right
retry-cascade tier fires for each of the three new branches:

1. probe inconclusive → first FFmpeg call uses ``use_skip=False``,
2. probe says unsafe → first FFmpeg call uses ``use_skip=False`` + WARN,
3. probe clears + post-extract validator detects duplicates → first
   FFmpeg call uses ``use_skip=True``, then a second call fires with
   ``use_skip=False`` (the validator-driven retry).

The integration tests deliberately do NOT use the autouse fixture from
``test_media_processing.py`` (different module, different scope), so the
real helpers and the real generate_images flow execute end-to-end with
only the subprocess + filesystem boundary mocked.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest
from loguru import logger

from media_preview_generator.processing.generator import (
    _has_duplicate_thumbnails,
    _probe_max_keyframe_gap,
    generate_images,
)


@pytest.fixture
def loguru_caplog(caplog):
    """Bridge loguru → pytest's caplog so the integration tests can
    assert on the user-facing WARN messages.  Mirrors the helper in
    ``test_version_check.py``.
    """

    class _PropagateHandler(logging.Handler):
        def emit(self, record):  # pragma: no cover — handler glue
            logging.getLogger(record.name).handle(record)

    handler_id = logger.add(_PropagateHandler(), level="DEBUG", format="{message}")
    caplog.set_level(logging.DEBUG)
    try:
        yield caplog
    finally:
        logger.remove(handler_id)


# ---------------------------------------------------------------------------
# _probe_max_keyframe_gap
# ---------------------------------------------------------------------------


def _ffprobe_stdout(rows: list[tuple[float, str]]) -> str:
    """Render synthetic ffprobe csv=p=0 output for ``packet=pts_time,flags``.

    ``rows`` is a list of (pts_time, flags) — e.g. ``(0.0, "K_")``,
    ``(0.042, "__")``.  Mirrors exactly what FFprobe emits in production.
    """
    return "\n".join(f"{ts:.6f},{flags}" for ts, flags in rows) + "\n"


def _mock_ffprobe(returncode: int, stdout: str) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = ""
    return proc


def test_probe_returns_max_gap_with_dense_keyframes():
    """Keyframes every ~1 s — max gap is small."""
    rows = [(t, "K_") for t in [0.0, 1.0, 2.0, 3.0, 4.0]]
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, _ffprobe_stdout(rows))
        gap = _probe_max_keyframe_gap("/fake/video.mkv")
    assert gap == pytest.approx(1.0)


def test_probe_returns_max_gap_with_sparse_keyframes():
    """The Dhurandhar pattern: irregular keyframes, max gap = 8.7s."""
    rows = [(t, "K_") for t in [0.0, 0.917, 5.0, 10.0, 18.708]]
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, _ffprobe_stdout(rows))
        gap = _probe_max_keyframe_gap("/fake/video.mkv")
    assert gap == pytest.approx(8.708, abs=0.01)


def test_probe_ignores_non_keyframe_packets():
    """Probe must only consider rows whose flags contain 'K'."""
    rows = [
        (0.0, "K_"),
        (0.042, "__"),
        (0.083, "__"),
        (1.0, "K_"),
        (1.042, "__"),
        (10.0, "K_"),
    ]
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, _ffprobe_stdout(rows))
        gap = _probe_max_keyframe_gap("/fake/video.mkv")
    # max gap is between keyframes at 1.0 and 10.0 = 9.0s
    assert gap == pytest.approx(9.0)


def test_probe_returns_none_on_ffprobe_error():
    """Non-zero return code means we can't trust the output."""
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(1, "")
        assert _probe_max_keyframe_gap("/fake/video.mkv") is None


def test_probe_returns_none_on_timeout():
    """ffprobe hangs → callers must treat it as unsafe."""
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffprobe", timeout=120)
        assert _probe_max_keyframe_gap("/fake/video.mkv") is None


def test_probe_returns_none_on_oserror():
    """ffprobe missing from PATH → still safe (returns None)."""
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError("ffprobe")
        assert _probe_max_keyframe_gap("/fake/video.mkv") is None


def test_probe_returns_none_when_no_keyframes():
    """Output has packets but none are keyframes — can't compute a gap."""
    rows = [(t, "__") for t in [0.0, 0.042, 0.083, 0.125]]
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, _ffprobe_stdout(rows))
        assert _probe_max_keyframe_gap("/fake/video.mkv") is None


def test_probe_returns_none_with_single_keyframe():
    """Need at least 2 keyframes to compute a gap."""
    rows = [(0.0, "K_")] + [(t, "__") for t in [0.042, 0.083]]
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, _ffprobe_stdout(rows))
        assert _probe_max_keyframe_gap("/fake/video.mkv") is None


def test_probe_handles_garbage_lines_gracefully():
    """Malformed lines must be skipped, not crash the parser."""
    stdout = "0.000000,K_\nthis is not a row\n,K_\n5.0,K_\n"
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, stdout)
        gap = _probe_max_keyframe_gap("/fake/video.mkv")
    assert gap == pytest.approx(5.0)


def test_probe_handles_compound_flags():
    """Real FFprobe sometimes emits 'K__', 'K_D', etc. — all are keyframes."""
    rows = [
        (0.0, "K__"),
        (3.0, "K_D"),
        (10.0, "K_"),
    ]
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, _ffprobe_stdout(rows))
        gap = _probe_max_keyframe_gap("/fake/video.mkv")
    assert gap == pytest.approx(7.0)


def test_probe_invokes_ffprobe_with_correct_args():
    """The command shape is part of the contract — full-file packet
    scan on the first video stream, no decode.

    Assert paired flags by adjacency so a reorder regression (e.g. moving
    ``-select_streams`` next to a different value) trips the test.  The
    timeout / check kwargs are also part of the contract: ffprobe must
    be bounded and must NOT raise on non-zero rc (the caller treats it
    as "unsafe" and falls through to the slow path).
    """
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, _ffprobe_stdout([(0.0, "K_"), (1.0, "K_")]))
        _probe_max_keyframe_gap("/some/file.mkv")
    args = mock_run.call_args[0][0]
    assert args[0] == "ffprobe"
    assert args[args.index("-select_streams") + 1] == "v:0"
    assert args[args.index("-show_entries") + 1] == "packet=pts_time,flags"
    assert args[args.index("-of") + 1] == "csv=p=0"
    assert "/some/file.mkv" in args
    # No -read_intervals — we deliberately scan the whole file (issue #238).
    assert "-read_intervals" not in args
    # Boundary contract: bounded timeout, never raises on non-zero rc.
    assert mock_run.call_args.kwargs["timeout"] == 120
    assert mock_run.call_args.kwargs["check"] is False


# ---------------------------------------------------------------------------
# _has_duplicate_thumbnails
# ---------------------------------------------------------------------------


def _write_jpgs(folder: Path, payloads: list[bytes]) -> None:
    """Write ``img-NNNNNN.jpg`` files with the given byte payloads."""
    folder.mkdir(parents=True, exist_ok=True)
    for i, data in enumerate(payloads, start=1):
        (folder / f"img-{i:06d}.jpg").write_bytes(data)


def test_no_duplicates_returns_false(tmp_path):
    """Every JPG distinct — well below the 5% threshold."""
    _write_jpgs(tmp_path, [f"unique-{i}".encode() for i in range(100)])
    assert _has_duplicate_thumbnails(str(tmp_path)) is False


def test_heavy_run_length_duplicates_returns_true(tmp_path):
    """The Dhurandhar signature: many adjacent dupes."""
    payloads: list[bytes] = []
    for i in range(50):
        payloads.append(f"frame-{i}".encode())
        payloads.append(f"frame-{i}".encode())  # dupe of previous
    _write_jpgs(tmp_path, payloads)
    assert _has_duplicate_thumbnails(str(tmp_path)) is True


def test_below_threshold_returns_false(tmp_path):
    """4% adjacent dupes — under the 5% default threshold."""
    payloads = [f"frame-{i}".encode() for i in range(100)]
    payloads[10] = payloads[9]
    payloads[30] = payloads[29]
    payloads[50] = payloads[49]
    # 3 dupes out of 100 = 3%
    _write_jpgs(tmp_path, payloads)
    assert _has_duplicate_thumbnails(str(tmp_path)) is False


def test_above_threshold_returns_true(tmp_path):
    """7% adjacent dupes — over threshold."""
    payloads = [f"frame-{i}".encode() for i in range(100)]
    for idx in range(1, 15, 2):
        payloads[idx] = payloads[idx - 1]
    # 7 dupes out of 100 = 7%
    _write_jpgs(tmp_path, payloads)
    assert _has_duplicate_thumbnails(str(tmp_path)) is True


def test_empty_folder_returns_false(tmp_path):
    """No JPGs to check — definitely not "more than threshold" duplicates."""
    assert _has_duplicate_thumbnails(str(tmp_path)) is False


def test_single_jpg_returns_false(tmp_path):
    """One image can't have an adjacent duplicate."""
    _write_jpgs(tmp_path, [b"only-one"])
    assert _has_duplicate_thumbnails(str(tmp_path)) is False


def test_only_glob_pattern_img_dash_is_considered(tmp_path):
    """The helper looks at ``img-*.jpg`` specifically — files renamed to
    the timestamp pattern (already-published runs) must not be scanned."""
    # Renamed files (post-rename pattern) shouldn't even be visible.
    (tmp_path / "0000000000.jpg").write_bytes(b"renamed-1")
    (tmp_path / "0000000002.jpg").write_bytes(b"renamed-1")  # dupe but ignored
    # No img-*.jpg at all → no pairs → returns False.
    assert _has_duplicate_thumbnails(str(tmp_path)) is False


def test_custom_threshold_respected(tmp_path):
    """Caller-supplied threshold flips the verdict accordingly."""
    payloads = [f"frame-{i}".encode() for i in range(100)]
    for idx in range(1, 11, 2):
        payloads[idx] = payloads[idx - 1]
    # 5 dupes out of 100 = 5%
    _write_jpgs(tmp_path, payloads)
    assert _has_duplicate_thumbnails(str(tmp_path), threshold=0.10) is False
    assert _has_duplicate_thumbnails(str(tmp_path), threshold=0.03) is True


def test_dupe_detection_matches_md5_signature(tmp_path):
    """Byte-identical files must hash the same and count as a dupe; a
    one-byte difference must not."""
    a = hashlib.md5(b"a" * 5000).digest()
    b = hashlib.md5(b"a" * 4999 + b"b").digest()
    assert a != b  # sanity
    payloads = [b"a" * 5000, b"a" * 5000, b"a" * 4999 + b"b"]
    _write_jpgs(tmp_path, payloads)
    # 1 dupe out of 3 = 33% — well over default threshold.
    assert _has_duplicate_thumbnails(str(tmp_path)) is True


# ---------------------------------------------------------------------------
# Integration tests — probe / validator wiring inside generate_images
# ---------------------------------------------------------------------------
#
# These exist because the unit tests above only cover the helpers in
# isolation.  The new branches at the top of generate_images() — and the
# new post-extract retry — need their own coverage so a future refactor
# that breaks the wiring (e.g. drops the validator, flips the did_retry
# bookkeeping) trips the suite.  Boundary mocks only: subprocess.run,
# subprocess.Popen, filesystem.  No autouse helper-patching.


def _make_integration_config(tmp_path):
    """Minimal Config mock matching what generate_images reads."""
    cfg = MagicMock()
    cfg.plex_bif_frame_interval = 2
    cfg.thumbnail_quality = 4
    cfg.tmp_folder = str(tmp_path)
    cfg.ffmpeg_path = "/usr/bin/ffmpeg"
    cfg.ffmpeg_threads = 2
    cfg.tonemap_algorithm = "hable"
    cfg.log_level = "INFO"
    cfg.server_display_name = None
    return cfg


def _wire_integration_mocks(
    *,
    mock_run,
    mock_popen,
    mock_mediainfo,
    mock_exists,
    mock_file,
    mock_detect,
    mock_glob,
    probe_stdout,
    temp_dir,
):
    """Set up the boundary mocks so generate_images runs end-to-end.

    ``probe_stdout`` is what ffprobe (subprocess.run) returns — the real
    ``_probe_max_keyframe_gap`` parses it just like in production.
    ``subprocess.Popen`` (FFmpeg) reports a clean rc=0 single-call run.
    """
    mock_run.return_value = MagicMock(returncode=0, stdout=probe_stdout, stderr="")

    mock_info = MagicMock()
    mock_info.video_tracks = [MagicMock(hdr_format=None)]
    mock_mediainfo.parse.return_value = mock_info

    mock_proc = MagicMock()
    # Two None/0 cycles so the validator-retry test gets a fresh poll
    # sequence for its second FFmpeg pass without StopIteration.
    mock_proc.poll.side_effect = [None, 0, None, 0]
    mock_proc.returncode = 0
    mock_popen.return_value = mock_proc

    mock_exists.return_value = True
    mock_file.return_value.readlines.return_value = []
    mock_detect.return_value = False

    img1 = f"{temp_dir}/img-000001.jpg"
    ts1 = f"{temp_dir}/0000000000.jpg"

    def glob_side_effect(pattern):
        if "img*.jpg" in pattern or "img-*.jpg" in pattern:
            return [img1]
        if pattern.endswith("*.jpg"):
            return [ts1]
        return []

    mock_glob.side_effect = glob_side_effect


@patch("media_preview_generator.processing.generator.MediaInfo")
@patch("subprocess.Popen")
@patch("media_preview_generator.processing.generator.subprocess.run")
@patch("media_preview_generator.processing.generator.os.rename")
@patch("media_preview_generator.processing.generator.os.remove")
@patch("os.path.exists")
@patch("builtins.open", new_callable=mock_open)
@patch("time.sleep")
@patch("media_preview_generator.processing.generator.glob.glob")
@patch("media_preview_generator.processing.generator._detect_codec_error")
@patch("media_preview_generator.processing.generator._has_duplicate_thumbnails")
def test_integration_probe_unsafe_disables_skip_frame(
    mock_has_dupes,
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
    tmp_path,
    loguru_caplog,
):
    """Probe returns max_gap > interval → first FFmpeg call drops
    ``-skip_frame:v nokey`` and the WARN log fires once.

    Mirrors the Dhurandhar production case: keyframes at 0 s and 10 s,
    user's interval is 2 s, gap exceeds interval → slow path required.
    """
    # Validator should never run when probe already disabled skip_frame.
    mock_has_dupes.return_value = False
    cfg = _make_integration_config(tmp_path)
    _wire_integration_mocks(
        mock_run=mock_run,
        mock_popen=mock_popen,
        mock_mediainfo=mock_mediainfo,
        mock_exists=mock_exists,
        mock_file=mock_file,
        mock_detect=mock_detect,
        mock_glob=mock_glob,
        probe_stdout="0.000000,K_\n10.000000,K_\n",  # 10s gap > 2s interval
        temp_dir=str(tmp_path),
    )

    with loguru_caplog.at_level("WARNING"):
        generate_images("/test/dhurandhar.mkv", str(tmp_path), None, None, cfg)

    # Exactly one FFmpeg call, and that call had NO -skip_frame flag.
    assert mock_popen.call_count == 1, "Probe-driven slow path must not retry FFmpeg"
    args = mock_popen.call_args_list[0][0][0]
    assert "-skip_frame:v" not in args, (
        "Probe said max_gap=10s > interval=2s — FFmpeg must run without "
        "-skip_frame:v nokey to produce unique thumbnails"
    )
    # User-friendly WARN line fired exactly once.
    warns = [r for r in loguru_caplog.records if r.levelname == "WARNING" and "Slow path" in r.message]
    assert len(warns) == 1, f"Expected one Slow-path WARN, got {len(warns)}"
    assert "snapshot frame every" in warns[0].message
    assert "10s" in warns[0].message  # the actual gap value, not the interval
    # Validator never consulted — we already knew the file was unsafe.
    mock_has_dupes.assert_not_called()


@patch("media_preview_generator.processing.generator.MediaInfo")
@patch("subprocess.Popen")
@patch("media_preview_generator.processing.generator.subprocess.run")
@patch("media_preview_generator.processing.generator.os.rename")
@patch("media_preview_generator.processing.generator.os.remove")
@patch("os.path.exists")
@patch("builtins.open", new_callable=mock_open)
@patch("time.sleep")
@patch("media_preview_generator.processing.generator.glob.glob")
@patch("media_preview_generator.processing.generator._detect_codec_error")
@patch("media_preview_generator.processing.generator._has_duplicate_thumbnails")
def test_integration_probe_inconclusive_disables_skip_frame(
    mock_has_dupes,
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
    tmp_path,
    loguru_caplog,
):
    """Probe returns no usable keyframes (corrupt headers, no I-frames)
    → ``_probe_max_keyframe_gap`` returns ``None`` → callers treat it
    as unsafe and disable -skip_frame.  This is the "can't verify the
    file is safe, so take the slow path" branch.
    """
    mock_has_dupes.return_value = False
    cfg = _make_integration_config(tmp_path)
    _wire_integration_mocks(
        mock_run=mock_run,
        mock_popen=mock_popen,
        mock_mediainfo=mock_mediainfo,
        mock_exists=mock_exists,
        mock_file=mock_file,
        mock_detect=mock_detect,
        mock_glob=mock_glob,
        probe_stdout="",  # ffprobe wrote nothing → no keyframes parsed
        temp_dir=str(tmp_path),
    )

    with loguru_caplog.at_level("WARNING"):
        generate_images("/test/unknown.mkv", str(tmp_path), None, None, cfg)

    assert mock_popen.call_count == 1
    args = mock_popen.call_args_list[0][0][0]
    assert "-skip_frame:v" not in args
    warns = [r for r in loguru_caplog.records if r.levelname == "WARNING" and "Slow path" in r.message]
    assert len(warns) == 1
    assert "couldn't read" in warns[0].message.lower()
    mock_has_dupes.assert_not_called()


@patch("media_preview_generator.processing.generator.MediaInfo")
@patch("subprocess.Popen")
@patch("media_preview_generator.processing.generator.subprocess.run")
@patch("media_preview_generator.processing.generator.os.rename")
@patch("media_preview_generator.processing.generator.os.remove")
@patch("os.path.exists")
@patch("builtins.open", new_callable=mock_open)
@patch("time.sleep")
@patch("media_preview_generator.processing.generator.glob.glob")
@patch("media_preview_generator.processing.generator._detect_codec_error")
@patch("media_preview_generator.processing.generator._has_duplicate_thumbnails")
def test_integration_validator_retry_fires_when_fast_path_duplicates(
    mock_has_dupes,
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
    tmp_path,
    loguru_caplog,
):
    """Probe clears the fast path, FFmpeg runs once with -skip_frame,
    but the post-extract validator finds duplicate thumbnails → second
    FFmpeg call fires without -skip_frame.  This is the belt-and-braces
    safety net for files whose probe-window looked fine but whose mid-
    file GOP turned out to be sparser than the interval.
    """
    mock_has_dupes.return_value = True  # validator fires the retry
    cfg = _make_integration_config(tmp_path)
    _wire_integration_mocks(
        mock_run=mock_run,
        mock_popen=mock_popen,
        mock_mediainfo=mock_mediainfo,
        mock_exists=mock_exists,
        mock_file=mock_file,
        mock_detect=mock_detect,
        mock_glob=mock_glob,
        # Keyframes 0.5s apart — probe says safe at interval=2.
        probe_stdout="0.000000,K_\n0.500000,K_\n1.000000,K_\n1.500000,K_\n",
        temp_dir=str(tmp_path),
    )

    with loguru_caplog.at_level("WARNING"):
        generate_images("/test/edgecase.mkv", str(tmp_path), None, None, cfg)

    # Exactly two FFmpeg calls: first WITH skip_frame, second WITHOUT.
    assert mock_popen.call_count == 2, "Validator-driven retry must produce exactly two FFmpeg calls"
    first_args = mock_popen.call_args_list[0][0][0]
    second_args = mock_popen.call_args_list[1][0][0]
    assert "-skip_frame:v" in first_args, "First pass should attempt the fast path"
    assert first_args[first_args.index("-skip_frame:v") + 1] == "nokey"
    assert "-skip_frame:v" not in second_args, "Retry must drop -skip_frame to produce unique thumbnails"
    # Validator was consulted exactly once (after first pass; not again after retry).
    mock_has_dupes.assert_called_once()
    # User-friendly WARN explains why we re-ran.
    warns = [r for r in loguru_caplog.records if r.levelname == "WARNING" and "Re-running" in r.message]
    assert len(warns) == 1
    assert "unusual snapshot spacing" in warns[0].message
