# Audit: tests/test_worker_naming.py — 9 tests, 3 classes

## TestFriendlyDeviceLabel

| Line | Test | Verdict |
|---|---|---|
| 20 | `test_short_nvidia_name_passes_through` | **Strong** — strict equality on simple NVIDIA passthrough |
| 24 | `test_long_intel_name_collapses_to_bracketed_marketing_string` | **Strong** — pins the lspci-string parsing (extract bracketed UHD Graphics 770). Catches regression that lets long lspci strings reach the panel and wrap rows. |
| 32 | `test_amd_radeon_label` | **Strong** — AMD bracketed-name parsing |
| 36 | `test_missing_name_falls_back_to_device_path` | **Strong** — info dict missing 'name' → falls back to device path (NOT empty / NOT crash) |
| 39 | `test_missing_everything_falls_back_to_GPU` | **Strong** — fully-degraded fallback to literal "GPU" |
| 42 | `test_simple_namespace_input_works` | **Strong** — accepts both dict and SimpleNamespace inputs (different call-sites in production) |

## TestWorkerLabels

| Line | Test | Verdict |
|---|---|---|
| 50 | `test_gpu_worker_label_format` | **Strong** — strict equality on 2 specific examples; catches format drift |
| 57 | `test_cpu_worker_label_format` | **Strong** — strict equality on CPU label format |

## TestAllProducersUseTheSameHelper

| Line | Test | Verdict |
|---|---|---|
| 73 | `test_no_inline_old_format_in_production_code` | **Strong (clever)** — grep-based test that scans 4 production files for forbidden inline f-string formats. Catches a refactor that re-introduces the divergent label formatting. Unusual but well-justified pattern. |

## Summary

- **9 tests**, all **Strong**
- Includes a clever cross-file static check (test #9) that catches contract violations at the source, not just at the call site
- Complete matrix of (vendor: NVIDIA/Intel/AMD) × (info: dict/namespace/missing)

**File verdict: STRONG.** No changes needed.
