# Audit: tests/test_gpu_ci.py — 7 tests, 2 classes

## TestFFmpegVersionCI

| Line | Test | Verdict |
|---|---|---|
| 19 | `test_get_ffmpeg_version_success` | **Strong** — strict tuple equality `(7, 1, 1)` against parsed real-world FFmpeg banner. Catches regex regression. |
| 30 | `test_get_ffmpeg_version_failure` | **Strong** — pins the `None` return on subprocess failure (callers branch on this). |
| 38 | `test_check_ffmpeg_version_sufficient` | **Strong** — pins True at the boundary (7.1.0). |
| 46 | `test_check_ffmpeg_version_insufficient` | **Strong** — pins False below boundary (6.9.0). Together with the row above, locks the comparison direction. |

## TestGPUFormattingCI

| Line | Test | Verdict |
|---|---|---|
| 57 | `test_format_gpu_info_nvidia` | **Strong** — strict equality on the full formatted string `"NVIDIA GeForce RTX 3080 (CUDA)"`. |
| 62 | `test_format_gpu_info_amd` | **Strong** — strict equality including the `(VAAPI - /dev/dri/renderD128)` suffix. Catches drift in render-node display. |
| 67 | `test_format_gpu_info_intel` | **Strong** — strict equality. Distinct cell vs AMD (different vendor → same VAAPI formatter; pins parity). |

## Summary

- **7 tests** — 7 Strong, 0 Weak / Bug-blind / Tautological
- Note: substantially overlaps `tests/test_basic.py::TestGPUDetection` (`test_format_gpu_info`, `test_ffmpeg_version_check`). The basic.py file consolidates the same checks; this file is mildly redundant but keeps the CI-specific scenario isolated.

**File verdict: STRONG.** No changes needed. Mild duplication with test_basic.py is intentional (CI smoke isolation).
