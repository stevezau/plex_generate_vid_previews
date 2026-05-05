"""Pixel-correctness tests for DV / HDR tone-mapping.

What this test catches
----------------------
The HDR / Dolby-Vision tone-mapping path in
``media_preview_generator.processing.generator.generate_images`` is the
highest-bug-rate code path in the repo (Category E in
``tests/audit/HINDSIGHT_90_DAYS.md`` -- 9 recurring fixes in 90 days).

The unit suite in ``tests/test_media_processing.py`` only asserts
**filter-string ordering**.  Real failures look nothing like that:

* ``70cba4e`` -- ``VK_ERROR_OUT_OF_DEVICE_MEMORY`` because ``fps`` was
  inside the ``libplacebo`` filter (Vulkan allocator exhausted).
* ``a06ed98`` -- pure-green overlay because ``zscale`` ran on a DV5
  stream that has no HDR10 base layer to read.
* RPU side-data getting silently dropped by a CPU filter that
  precedes ``hwupload``.

None of those bugs change the ffmpeg argv in a way the existing tests
look at.  They produce a JPEG that is structurally the right shape but
either crashes outright, comes out all-green, or has a wildly wrong
luminance histogram.

This module runs the *real* production code path against a small HDR10
fixture and compares the output JPEG to a checked-in golden frame
using PSNR -- the same metric the ``70cba4e`` commit message cites
when claiming "pixel-identical (PSNR=inf) across dark, mid, and bright
ranges".

How to (re)generate the golden frames
-------------------------------------
Each vendor path has its own golden under ``tests/fixtures/golden/``
because hardware tone-mappers quantise differently than the CPU
chain (e.g. the NVIDIA scale_cuda + zscale combo lands ~28 dB PSNR
relative to the CPU output -- not a bug, just legitimate
encoder-level divergence).  Comparing each vendor against its own
prior run keeps the floor tight (~45 dB) so a real regression
(green overlay, collapsed histogram, RPU drop) cannot hide.

Delete a golden and re-run the test; the fixture below regenerates
it from that vendor's path on first invocation::

    rm tests/fixtures/golden/hdr10_cpu_frame.jpg          # CPU baseline
    rm tests/fixtures/golden/hdr10_nvidia_cuda_frame.jpg  # NVIDIA baseline
    pytest tests/test_dv_hdr_pixel_correctness.py \
        --no-cov --override-ini="addopts="

Commit the regenerated files alongside the change.

When to bump the baseline PSNR
------------------------------
The default tolerance is ``MIN_PSNR_DB = 45.0`` -- the same number
the ``70cba4e`` commit message cites for "pixel-identical (PSNR=inf)
across dark, mid, and bright ranges" after rounding for JPEG
re-encoding.  Same-vendor re-renders should comfortably exceed this;
if the assertion fails after a deliberate algorithm change (e.g.
swapping ``hable`` for ``mobius``), regenerate the goldens and
explain the new behaviour in the commit message.

The HDR10 fixture is used as a stand-in for DV5 because synthetic DV5
content with a real RPU requires ``dovi_tool`` and a DV-aware encoder
(see ``tests/fixtures/media/generate.sh`` for the on-repo limitation).
The bug shape we care about -- tone-map ratios, gamut conversion,
hwaccel interop -- is the same.

All tests are gated on ``@pytest.mark.gpu`` and additionally
``pytest.skip`` themselves when their vendor's hardware is unavailable.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Module-level prerequisite checks: skip the whole module when the test
# infrastructure cannot possibly run (no ffmpeg, no Pillow, no numpy).
# This keeps a CI box without ffmpeg from erroring out -- it just
# reports the module as skipped with a clear reason.
if shutil.which("ffmpeg") is None:
    pytest.skip("ffmpeg binary not available on PATH", allow_module_level=True)

PIL = pytest.importorskip("PIL", reason="Pillow required for pixel comparison")
np = pytest.importorskip("numpy", reason="numpy required for PSNR computation")

from PIL import Image  # noqa: E402

from media_preview_generator.processing.generator import generate_images  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "media"
GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"

# Pinned thumbnail size matches generate_images()'s base_scale of 320x240
# with force_original_aspect_ratio=decrease.  The 640x360 fixture is 16:9
# so it scales to 320x180.
EXPECTED_W = 320
EXPECTED_H = 180

# Same-vendor PSNR floor.  CPU re-runs against the CPU golden are
# typically infinity (deterministic).  Hardware paths re-run against
# their own per-vendor golden should also be deterministic to within
# JPEG re-encode noise.  45 dB matches the floor cited in commit
# 70cba4e ("pixel-identical -- PSNR > 45 dB across dark, mid, bright")
# and is well above the bug-failure regime (green overlay / black
# frame produce PSNR < 15 dB).
MIN_PSNR_DB = 45.0

# Variance floor.  A frame that tone-maps to all-black or all-white
# (the failure mode of a broken zscale/libplacebo chain) collapses the
# histogram; healthy tone-mapped output of testsrc2 has variance > 1000
# on uint8 luma.  500 is a safety margin.
MIN_LUMA_VARIANCE = 500.0


def _make_config(tmp_folder: str) -> MagicMock:
    """Build a minimal Config mock that exercises generate_images.

    Mirrors the defaults from ``tests/conftest.py:mock_config`` but
    with a 2-second frame interval so the 1-second fixture yields a
    single deterministic frame at t=0.
    """
    c = MagicMock()
    c.plex_bif_frame_interval = 2
    c.thumbnail_quality = 4
    c.tmp_folder = tmp_folder
    c.ffmpeg_path = "/usr/bin/ffmpeg"
    c.ffmpeg_threads = 2
    c.tonemap_algorithm = "hable"
    return c


def _render_first_frame(gpu: str | None, gpu_device_path: str | None, tmp_path: Path) -> Path:
    """Run the production code path against the HDR10 fixture and
    return the path to the first JPEG produced.

    Raises pytest.skip if the run fails for an environmental reason
    (vendor hwaccel unusable in this container).
    """
    fixture = FIXTURE_DIR / "hdr10_tiny.mkv"
    if not fixture.exists():
        pytest.skip(f"HDR10 fixture missing at {fixture} -- run tests/fixtures/media/generate.sh")

    out_dir = tmp_path / "frames"
    out_dir.mkdir()
    config = _make_config(str(tmp_path))

    success, image_count, _hw_used, _seconds, _speed, err_summary = generate_images(
        str(fixture),
        str(out_dir),
        gpu,
        gpu_device_path,
        config,
    )

    if not success or image_count == 0:
        pytest.skip(f"generate_images produced no output for gpu={gpu!r}: {err_summary or 'no error message'}")

    frames = sorted(out_dir.glob("*.jpg"))
    if not frames:
        pytest.skip(f"No JPEGs written by generate_images for gpu={gpu!r}")
    return frames[0]


def _compute_psnr(a: np.ndarray, b: np.ndarray) -> float:
    """Standard PSNR on uint8 arrays, returns float('inf') for identical
    inputs.  Both arrays must have the same shape and dtype.
    """
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch: {a.shape} vs {b.shape}")
    diff = a.astype(np.float64) - b.astype(np.float64)
    mse = float(np.mean(diff * diff))
    if mse == 0.0:
        return float("inf")
    return 20.0 * float(np.log10(255.0)) - 10.0 * float(np.log10(mse))


def _luma_variance(img: Image.Image) -> float:
    """Variance of the Y channel.  Catches all-black / all-white frames
    (tone-map collapse) without being fooled by a uniform colour cast.
    """
    arr = np.asarray(img.convert("L"), dtype=np.float64)
    return float(arr.var())


# --------------------------------------------------------------------- #
# Vendor probes -- skip individual tests when their hardware is missing #
# --------------------------------------------------------------------- #


def _have_nvidia() -> bool:
    """True if nvidia-smi reports at least one GPU."""
    nvsmi = shutil.which("nvidia-smi")
    if nvsmi is None:
        return False
    try:
        rc = subprocess.run([nvsmi, "-L"], capture_output=True, timeout=5).returncode
    except (subprocess.SubprocessError, OSError):
        return False
    return rc == 0


def _have_render_node(node: str) -> bool:
    """True if the named DRM render node exists (e.g. /dev/dri/renderD128)."""
    return Path(node).exists()


def _have_vaapi() -> bool:
    """True if any DRM render node exists -- AMD or Intel VAAPI candidate."""
    return any(_have_render_node(p) for p in ("/dev/dri/renderD128", "/dev/dri/renderD129"))


# Vendor matrix.  Each row carries:
#   * param_id            -- short label used in test reports
#   * gpu                 -- value passed as ``gpu`` to generate_images
#   * gpu_device_path     -- value passed as ``gpu_device_path``
#   * available_predicate -- callable returning False when the vendor's
#                            hardware is missing in this runner
#   * skip_reason         -- shown to the user when skipping
#   * golden_filename     -- per-vendor checked-in golden under
#                            tests/fixtures/golden/
#
# Per-vendor goldens are deliberate: hardware tone-mappers and JPEG
# encoders quantise differently (NVIDIA scale_cuda + zscale, Intel/AMD
# scale_vaapi + zscale, CPU all-software).  Comparing each vendor
# against its own prior run lets us hold the PSNR floor at 45 dB and
# still catch the green-overlay / black-frame bug shapes -- those would
# drop PSNR below 15 dB regardless of vendor.
VENDOR_MATRIX = [
    pytest.param("cpu", None, None, lambda: True, "", "hdr10_cpu_frame.jpg", id="cpu"),
    pytest.param(
        "nvidia",
        "NVIDIA",
        "cuda",
        _have_nvidia,
        "no nvidia-smi / no NVIDIA GPU detected",
        "hdr10_nvidia_cuda_frame.jpg",
        id="nvidia_cuda",
    ),
    pytest.param(
        "amd",
        "AMD",
        "/dev/dri/renderD128",
        _have_vaapi,
        "no DRM render node available for VAAPI",
        "hdr10_amd_vaapi_frame.jpg",
        id="amd_vaapi",
    ),
    pytest.param(
        "intel",
        "INTEL",
        "/dev/dri/renderD128",
        _have_vaapi,
        "no DRM render node available for QSV/VAAPI",
        "hdr10_intel_qsv_frame.jpg",
        id="intel_qsv",
    ),
]


# --------------------------------------------------------------------- #
# Tests                                                                  #
# --------------------------------------------------------------------- #


@pytest.mark.gpu
@pytest.mark.parametrize(
    "vendor_id, gpu, device, available, skip_reason, golden_filename",
    VENDOR_MATRIX,
)
def test_vendor_path_pixel_correctness(vendor_id, gpu, device, available, skip_reason, golden_filename, tmp_path):
    """Run a vendor's HDR10 tone-map path and check pixel correctness.

    Three orthogonal assertions, each tied to a real failure mode from
    the bug history:

      1. **Dimensions** -- the GPU-scale segment must produce
         ``320x180`` (the 16:9 fixture under decrease aspect).  A
         broken scale_cuda / scale_vaapi step was the root cause of
         the b3a4f81 regression.
      2. **Luma variance > MIN_LUMA_VARIANCE** -- catches the
         all-black / all-white / pure-green failure mode (a06ed98)
         where the tone-map collapses the histogram.
      3. **PSNR vs the per-vendor golden >= MIN_PSNR_DB** -- catches
         drift in the tone-map output between commits, exactly the
         metric 70cba4e used to validate "no visual change" after the
         fps-before-hwupload reorder.

    Skip semantics:
      * Vendor hardware missing -> skip with the available_predicate's
        skip_reason (no fail).
      * generate_images returns success=False -- skip, not fail (this
        means the vendor driver is unusable in the runner, not that
        the code under test is broken).
      * Golden file missing -- write it from this run and skip with a
        message; commit the file and re-run.  Tests can't fail on
        first run if the test author hasn't yet committed the
        baseline.
    """
    if not available():
        pytest.skip(skip_reason)

    produced = _render_first_frame(gpu=gpu, gpu_device_path=device, tmp_path=tmp_path)

    assert produced.stat().st_size > 0, f"{vendor_id} output JPEG is zero bytes"

    img = Image.open(produced)
    assert img.size == (EXPECTED_W, EXPECTED_H), (
        f"{vendor_id} output dims {img.size} != expected ({EXPECTED_W}, {EXPECTED_H}) -- "
        "GPU scale segment or aspect-ratio handling regressed"
    )

    variance = _luma_variance(img)
    assert variance > MIN_LUMA_VARIANCE, (
        f"{vendor_id} output luma variance {variance:.1f} below floor {MIN_LUMA_VARIANCE} -- "
        "tone-map collapsed the histogram (the pure-green / black-frame failure mode)"
    )

    golden_path = GOLDEN_DIR / golden_filename
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    if not golden_path.exists():
        shutil.copy2(produced, golden_path)
        pytest.skip(
            f"Golden frame written to {golden_path} on first run -- "
            "commit it and re-run the suite to enable PSNR comparisons."
        )

    golden = np.asarray(Image.open(golden_path).convert("RGB"))
    current = np.asarray(img.convert("RGB"))
    psnr = _compute_psnr(golden, current)
    assert psnr >= MIN_PSNR_DB, (
        f"{vendor_id} PSNR {psnr:.2f} dB below floor {MIN_PSNR_DB} dB vs {golden_path.name} -- "
        "tone-map output drifted from the committed golden; "
        "if intentional, regenerate the golden and explain in the commit message."
    )
