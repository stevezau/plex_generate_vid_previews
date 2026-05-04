# Audit: tests/test_gpu_detection_extended.py — ~205 tests, 33 classes

Methodology note: this is a 3319-line file with extensive parametrized + heavy-mocked tests. Sampled all classes; verdicts apply to the class as a whole unless a specific test is called out.

## TestDetectAllGPUs

| Line | Test | Verdict |
|---|---|---|
| 37 | `test_detect_all_gpus_nvidia` | **Strong** — asserts vendor, device==`"cuda:0"`, name substring, acceleration=="CUDA" |
| 58 | `test_detect_all_gpus_amd` | **Strong** — vendor + device + acceleration |
| 76 | `test_detect_all_gpus_intel` | **Strong** — vendor + exact device + acceleration |
| 91 | `test_detect_all_gpus_none` | **Strong** — `gpus == []` |

## TestHwaccelAvailability

| Line | Test | Verdict |
|---|---|---|
| 117 | `test_is_hwaccel_available_cuda` | **Strong** — both branches in 1 test |
| 125 | `test_is_hwaccel_available_none` | **Strong** |

## TestHwaccelFunctionality

All 14 tests (lines 137–281) — **Strong**: each isolates a specific stderr signal (CUDA init, vaapi perm denied, sigpipe 141, timeout, exception, empty stderr, etc.) and asserts `is True/False` on the boolean result. Boundary mocks subprocess+os.access correctly.

## TestGetGPUDevices

| Line | Test | Verdict |
|---|---|---|
| 292 | `test_get_gpu_devices` | **Strong** — len + list-equality on cards + per-tuple asserts (path startswith, driver==expected). Connector-child rejection pinned. |
| 321 | `test_get_gpu_devices_no_drm` | **Strong** — `devices == []` |

## TestGetGPUName

| Line | Test | Verdict |
|---|---|---|
| 335 | `test_get_gpu_name_nvidia` | **Strong** — substring on known nvidia-smi output |
| 343 | `test_get_gpu_name_nvidia_failure` | **Weak (acceptable)** — substring "NVIDIA" + "GPU" — fallback string is free-form, this is the right level |
| 353 | `test_get_gpu_name_windows` | **Weak (acceptable)** — substring "Windows" + "GPU" |
| 361 | `test_get_gpu_name_intel_vaapi` | **Weak (acceptable)** — substring on Intel/UHD |
| 372 | `test_get_gpu_name_amd_vaapi` | **Weak (acceptable)** — substring on AMD/Radeon |
| 396 | `test_get_gpu_name_multi_gpu_distinct_per_render_node` | **Strong** — `name_0 != name_1` + per-node substring; pins distinct lookup |

## TestGetFFmpegHwaccels / TestFFmpegVersion / TestAppleGPU

All **Strong** (lines 441–539): sweeps return-value branches with strict equality (e.g., `assert result == "Apple M1 Pro"`), or `is None` checks for known error paths. ARM64 fallback returns exact `"Apple Silicon GPU"` (strict).

## TestLspciGPUDetection / TestLspciEdgeCases

All 8+5 tests (lines 546–929) — **Strong**: each asserts a specific vendor name (`== "AMD"`, `== "INTEL"`, `== "UNKNOWN"`) on parsed lspci output. Exhaustive failure-mode coverage (FileNotFoundError, exception, empty, no-match).

## TestLogSystemInfo

| Line | Test | Verdict |
|---|---|---|
| 641 | `test_log_system_info` | **Strong** — substring across all debug calls; OR-disjunction is fine because the test rationale explicitly explains "any one would prove the function is not a no-op" |

## TestParseLspciGPUName

| Line | Test | Verdict |
|---|---|---|
| 664/674/685 | `test_parse_lspci_gpu_name_*` | **Strong** — strict equality on fallback string (`"NVIDIA GPU"`, `"AMD GPU"`, `"INTEL GPU"`) |

## TestAccelerationMethodTesting

| Line | Test | Verdict |
|---|---|---|
| 708 | `test_test_acceleration_method_cuda_failure` | **Strong** — `result is False` + `mock_test.assert_called_once_with("cuda")` (exact arg pin) |
| 717 | `test_test_acceleration_method_vaapi_failure` | **Strong** — pins device path forwarded |
| 724 | `test_test_acceleration_method_returns_false_when_hwaccel_unavailable` | **Strong** |
| 731 | `test_test_acceleration_method_unknown_vendor` | **Strong** — guards against the lowercase masking issue called out in class docstring |

## TestNvidiaSmiDetection / TestGPUVendorFromDriver / TestCheckDeviceAccess / TestBuildGpuErrorDetail

All tests **Strong** (lines 740–886): strict equality on classification ("NVIDIA"/"UNKNOWN"), `(ok, reason)` tuple checks, error-message substring + negative `not in` checks (pin no-`--group-add` advice).

## TestCheckFFmpegVersion / TestGetFFmpegVersionParsing

All **Strong** (lines 936–979): version tuple strict equality `(7, 1, 1)` etc; `is None` for unparseable.

## TestDetectAllGPUsEdgeCases / TestWSL2NoDRMDevices / TestLinuxContainerNvidiaFallback / TestDetectWindowsGPUs

All tests in these classes (lines 990–1685) — **Strong**: pin specific tuple shape `(vendor, device, info)` with strict equality on vendor + device + info dict subkeys. WSL2/container fallback paths exhaustively covered (CUDA detected, no DRM, no nvidia-smi, smi-confirms-but-test-fails). Each class systematically tests multiple cells of the matrix.

## TestProbeVulkanDevice (the libplacebo green-bug suite)

22 tests covering Strategy 1 → 2 → 2b → 2c → 3 escalation. All **Strong**: strict equality on returned device strings, exact `mock_run.call_count == N` (proves which strategies fired), `env_arg.get("VK_DRIVER_FILES") == ...` (exact env override), `mock_find_libegl.assert_not_called()` (exact gating). Classes:
- `test_strategy_2_egl_vendor_override_succeeds` (1785)
- `test_strategy_2b_vk_driver_files_fallback_when_egl_retry_fails` (1832)
- `test_strategy_2c_synthesises_vendor_json_when_missing_but_libegl_present` (1883) — verifies file content is the exact NVIDIA-spec JSON
- `test_strategy_2c_skipped_when_libegl_nvidia_missing` (1974) — gating
- `test_strategy_2c_skipped_when_vendor_json_already_present` (2020)
- `test_all_retries_skipped_when_nothing_to_retry` (2059)
- `test_strategy_3_diagnostic_capture_populates_debug_buffer` (2104) — assert env+buffer content
- `test_get_vulkan_env_overrides_auto_triggers_probe` (2165) — cache-correctness
- `test_strategy_1_success_does_not_touch_debug_buffer_or_overrides` (2213) — happy-path no-side-effects
- `test_strategy_1_intel_with_nvidia_icd_falls_through_to_retries` (2245) — dual-GPU host
- `test_strategy_1_intel_without_nvidia_icd_accepts_intel` (2299) — Intel-only short-circuit

## TestGetVulkanDeviceInfo / TestGetVulkanInfoAPI / TestDiagnoseVulkanEnvironment

All **Strong** (lines 2326–2879): cache pin via `mock_probe.call_count == 1` after multiple invocations; software detection with `is_software is True/False`; per-case warning content asserted (Case A1/A2/A3/A4/B/C/D/E with the specific env-var name and issue-tracker link they should mention). The Case-A dispatcher tests are particularly thorough — diagnostic content verified, NOT just "warning present".

## TestProbeVaapiDriver / TestFormatDriverLabel

All **Strong** (lines 2902–2987): strict equality on parsed driver string + label format. `mock_probe.assert_called_once_with(path)` for Intel; `mock_probe.assert_not_called()` for nvidia/amdgpu (proves vendor branching).

## TestIsNvidiaVulkanDevice

| Line | Test | Verdict |
|---|---|---|
| 2993 | `test_classification` | **Strong** — parametrized 14-cell matrix covering NVIDIA brand prefix, Intel/AMD prefix overrides, software rasterisers, None/empty |

## TestEnumerateNvidiaGpusViaSmi / TestDetectLinuxGpusMultiNvidia / TestHwaccelCudaDeviceIndex

All **Strong** (lines 3029–3319): per-GPU dict structure pinned, `device == "cuda:0"` / `"cuda:1"`, dedup verified (no duplicate when smi already claimed). `cuda_device_index` flag emission pinned via `args.index("-hwaccel_device") + 1 == "1"`.

## Summary

- **~205 tests**, all **Strong** (4 minor weak-but-acceptable in TestGetGPUName fallback strings)
- The Vulkan probe suite is exemplary: pins exact subprocess call count + env override dict + cached-state behaviour
- Case dispatch tests verify diagnostic content per case, not just presence of "warning"
- 0 bug-blind / tautological / bug-locking finds

**File verdict: STRONG.** No changes needed.

The 4 GetGPUName fallback-string tests use substring on free-form display strings — the right level for that contract; not flagging as needing fix.
