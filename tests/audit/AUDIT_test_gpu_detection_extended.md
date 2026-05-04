# Audit: tests/test_gpu_detection_extended.py — adversarial re-review

3,319 lines, ~205 tests, 33 classes. The previous single-pass audit verdicted the file STRONG with 4 minor weak-but-acceptable cells. This re-review is adversarial: assume the previous verdict missed cases of:
1. tautological subprocess.run mocks (mock returns text, SUT returns same text — no parsing exercised)
2. bug-blind subprocess: `mock_run.assert_called_once()` without checking the FFmpeg/lspci/nvidia-smi cmd arguments
3. weak GPU assertions (`isinstance` / `is not None` instead of pinning vendor+name+index)
4. matrix gaps across NVIDIA / AMD / Intel / Apple branches
5. log-side-effect tests where the assertion accepts literal format-string text rather than the mocked value

SUT references:
- `media_preview_generator/gpu/detect.py` (953 lines — `_test_hwaccel_functionality`, `_test_acceleration_method`, `_detect_windows_gpus`, `_detect_linux_gpus`, `detect_all_gpus`)
- `media_preview_generator/gpu/enumeration.py` (569 lines — `get_gpu_name`, `_enumerate_nvidia_gpus_via_smi`, `_detect_nvidia_via_nvidia_smi`, `_detect_gpu_type_from_lspci`, `_parse_lspci_gpu_name`, `_get_apple_gpu_name`, `_log_system_info`)
- `media_preview_generator/gpu/ffmpeg_capabilities.py`, `vulkan_probe.py`, `vaapi_probe.py`

## TestDetectAllGPUs

| Line | Test | Verdict | Note |
|---|---|---|---|
| 37 | `test_detect_all_gpus_nvidia` | Strong | vendor + device==`"cuda:0"` + name substring + acceleration=="CUDA" |
| 58 | `test_detect_all_gpus_amd` | Strong | vendor + device contains path + acceleration |
| 76 | `test_detect_all_gpus_intel` | Strong | vendor + exact device + acceleration |
| 91 | `test_detect_all_gpus_none` | Strong | gpus == [] with platform mocked |

## TestHwaccelAvailability

| Line | Test | Verdict | Note |
|---|---|---|---|
| 117 | `test_is_hwaccel_available_cuda` | Strong | both branches in 1 test |
| 125 | `test_is_hwaccel_available_none` | Strong | empty list → all False |

## TestHwaccelFunctionality

| Line | Test | Verdict | Note |
|---|---|---|---|
| 137 | `test_test_hwaccel_functionality_cuda_success` | **Bug-blind** — see "Why downgraded" | Mocks `subprocess.run` to returncode=0 and asserts `result is True`. Does NOT check that the cmd built by SUT contains `-hwaccel cuda`, `-i <test_video>`, output sink, etc. A regression that drops `-hwaccel` from the probe cmd entirely (so the probe falls back to software decode and "passes") would still return True and silently report the GPU as working. **Why downgraded:** the SUT at `gpu/detect.py:234-280` builds a non-trivial cmd with branching per hwaccel type (cuda/vaapi/d3d11va/videotoolbox). None of the success-path tests verify the cmd. The CUDA-index test at line 3300 DOES verify cmd structure — that's the model these should follow. |
| 144 | `test_test_hwaccel_functionality_cuda_failure` | Bug-blind (mild) | returncode=1 with arbitrary stderr → False. Doesn't pin cmd. Same risk as above but lower (failure path) — flagged for completeness |
| 152 | `test_test_hwaccel_functionality_cuda_devnull_error` | Bug-blind (mild) | Same; pins specific stderr substring is good but cmd not checked |
| 163 | `test_test_hwaccel_functionality_cuda_init_error` | Bug-blind (mild) | Same |
| 173 | `test_test_hwaccel_functionality_vaapi_success` | **Bug-blind** | Same issue: `result is True` from rc=0; no check that `-vaapi_device /dev/dri/renderD128` is in the cmd. **Why downgraded:** SUT at `detect.py:241-242` adds `-vaapi_device device_path`; test cannot detect a regression that drops the device flag. |
| 184 | `test_test_hwaccel_functionality_vaapi_device_not_found` | Strong | exists=False → False (early-return path, no subprocess.run, so no cmd to pin) |
| 192 | `test_test_hwaccel_functionality_vaapi_permission_denied` | Strong | exists+access mocked, no subprocess (early return), result False |
| 206 | `test_test_hwaccel_functionality_vaapi_stderr_error` | Bug-blind (mild) | Same cmd-not-pinned issue |
| 221 | `test_test_hwaccel_functionality_timeout` | Strong | TimeoutExpired → False (exception branch) |
| 231 | `test_test_hwaccel_functionality_d3d11va_error` | Bug-blind (mild) | Same |
| 239 | `test_test_hwaccel_functionality_sigpipe` | Strong | rc=141 accepted as success — SUT branch coverage |
| 247 | `test_test_hwaccel_functionality_exception` | Strong | RuntimeError → False (exception branch) |
| 255 | `test_test_hwaccel_functionality_empty_stderr` | Strong | rc=1 + empty stderr → False |
| 263 | `test_test_hwaccel_functionality_stderr_with_empty_lines` | Strong | rc=1 + whitespace stderr → False (defensive parse) |
| 271 | `test_test_hwaccel_functionality_vaapi_generic_error` | Bug-blind (mild) | Same |

## TestGetGPUDevices

| Line | Test | Verdict | Note |
|---|---|---|---|
| 292 | `test_get_gpu_devices` | Strong | len == 2, sorted card names, render-path startswith `/dev/dri/renderD`, driver pinned. Connector-child rejection pinned. |
| 321 | `test_get_gpu_devices_no_drm` | Strong | `devices == []` when /sys/class/drm absent |

## TestGetGPUName

| Line | Test | Verdict | Note |
|---|---|---|---|
| 335 | `test_get_gpu_name_nvidia` | Strong | nvidia-smi mock returns "NVIDIA GeForce RTX 3080\n"; SUT splits/strips/returns first line. Test asserts "RTX 3080" in result — exercises the parser, not just mock echo. |
| 343 | `test_get_gpu_name_nvidia_failure` | Weak (acceptable) | rc=1 → fallback "NVIDIA GPU"; substring check on free-form fallback string is appropriate |
| 353 | `test_get_gpu_name_windows` | Weak (acceptable) | Static "Windows GPU" string; substring check fine |
| 361 | `test_get_gpu_name_intel_vaapi` | Strong | Goes through `_parse_lspci_gpu_name` parsing path (PCI lookup likely fails in test env, fallback to lspci scan). Substring "Intel" or "UHD" — both possible since lspci returned line trimmed |
| 372 | `test_get_gpu_name_amd_vaapi` | Strong | Same shape as Intel — substring "AMD" or "Radeon" |
| 396 | `test_get_gpu_name_multi_gpu_distinct_per_render_node` | Strong | name_0 != name_1 PLUS per-node substring. Genuine end-to-end test of the per-PCI-address lookup path. |

## TestGetFFmpegHwaccels

| Line | Test | Verdict | Note |
|---|---|---|---|
| 441 | `test_get_ffmpeg_hwaccels` | Strong | Header line "Hardware acceleration methods:" excluded — pins the parser drops it |
| 458 | `test_get_ffmpeg_hwaccels_failure` | Strong | rc=1 → [] |

## TestFFmpegVersion

| Line | Test | Verdict | Note |
|---|---|---|---|
| 472 | `test_get_ffmpeg_version_parse_error` | Strong | invalid output → None |
| 481 | `test_get_ffmpeg_version_error` | Strong | exception → None |
| 491 | `test_check_ffmpeg_version_none` | Strong | None version → True (don't fail-stop on unknown) |

## TestAppleGPU

| Line | Test | Verdict | Note |
|---|---|---|---|
| 506 | `test_get_apple_gpu_name_success` | Strong | "Chipset Model: Apple M1 Pro" → assert == "Apple M1 Pro". SUT splits at ":" and trims — real parser exercise |
| 519 | `test_get_apple_gpu_name_error` | Strong | exception → fallback contains "Apple" |
| 529 | `test_get_apple_gpu_name_arm64_fallback` | Strong | rc=0 + no chipset + machine="arm64" → exact "Apple Silicon GPU" |

## TestLspciGPUDetection

8 tests (lines 546–632) — all Strong. Each asserts a specific vendor classification (`== "AMD"`, `== "INTEL"`, `== "NVIDIA"`, `== "ARM"`, `== "UNKNOWN"`) on parsed lspci output. Covers FileNotFoundError, generic exception, no-VGA-line, empty stdout, no-match.

## TestLogSystemInfo

| Line | Test | Verdict | Note |
|---|---|---|---|
| 641 | `test_log_system_info` | **Bug-blind** — see "Why downgraded" | Asserts `"Linux" in combined or "5.15.0" in combined or "Platform" in combined or "Kernel" in combined`. **Why downgraded:** the SUT at `enumeration.py:531-535` calls `platform.platform()`, `platform.python_version()`, `os.environ.get(...)` — NOT `platform.system()` / `platform.release()` directly. The test mocks `platform.system` and `platform.release` but those are not what the SUT calls. The OR-disjunction includes the literal label `"Platform"` which is in the SUT's format string `"Platform: {}"` — so the test passes regardless of whether the mocked values flow through. The function could log nothing about the OS at all and the test would still pass on the literal "Platform" in the format string. |

## TestParseLspciGPUName

| Line | Test | Verdict | Note |
|---|---|---|---|
| 663 | `test_parse_lspci_gpu_name_nvidia` | **Weak** — see "Why downgraded" | Mocks `subprocess.run` rc=1 so the SUT skips lspci parsing entirely and returns the fallback `"NVIDIA GPU"`. **Why downgraded:** this only tests the fallback branch (`return f"{gpu_type} GPU"`). The actual parsing path at `enumeration.py:391-396` (split lspci line on ":", take parts[2].strip()) is never exercised. A regression in that parser would not be caught by this test class. The success path is indirectly tested via `test_get_gpu_name_intel_vaapi` (line 361) but that's coincidental — `TestParseLspciGPUName` itself is misnamed/under-covering. |
| 674 | `test_parse_lspci_gpu_name_amd` | Weak | Same — fallback only |
| 685 | `test_parse_lspci_gpu_name_intel` | Weak | Same |

## TestAccelerationMethodTesting

| Line | Test | Verdict | Note |
|---|---|---|---|
| 708 | `test_test_acceleration_method_cuda_failure` | Strong | result False + `mock_test.assert_called_once_with("cuda")` — exact arg pinning |
| 717 | `test_test_acceleration_method_vaapi_failure` | Strong | exact arg pin includes device path |
| 724 | `test_test_acceleration_method_returns_false_when_hwaccel_unavailable` | Strong | early return, no probe |
| 731 | `test_test_acceleration_method_unknown_vendor` | Strong | lowercase masking guard (called out in class docstring) |

## TestNvidiaSmiDetection

7 tests (lines 740–785) — all Strong. Strict equality on classification ("NVIDIA"/"UNKNOWN"); FileNotFoundError, rc=1, empty, TimeoutExpired, RuntimeError all branch-tested.

## TestGPUVendorFromDriver

| Line | Test | Verdict | Note |
|---|---|---|---|
| 791 | `test_known_drivers` | Strong | nvidia/amdgpu/i915 → vendor strings |
| 802 | `test_unknown_driver_uses_lspci` | Strong | unknown driver delegates to lspci |

## TestCheckDeviceAccess / TestBuildGpuErrorDetail

8 tests (lines 814–885) — all Strong. (ok, reason) tuples pinned; permission-denied + group-membership matrix; no-`--group-add` advice (negative pin) + `host device permissions` (positive); device-not-found suggests `--device`.

## TestLspciEdgeCases / TestCheckFFmpegVersion / TestGetFFmpegVersionParsing

13 tests (lines 891–979) — all Strong. version-tuple strict equality, is None for unparseable.

## TestDetectAllGPUsEdgeCases

| Line | Test | Verdict | Note |
|---|---|---|---|
| 990 | `test_detect_all_gpus_macos_videotoolbox` | Strong | vendor="APPLE" + name substring "M1 Max" |
| 1017 | `test_detect_all_gpus_nvidia_nvenc` | Strong | NVIDIA via DRM with both CUDA + NVENC True; len >= 1 (loose count is fine here since the SUT may yield 1 or 2 entries depending on dedup logic) |

## TestWSL2NoDRMDevices

10 tests (lines 1072–1397) — all Strong. Each pins `(vendor, device, info)` tuple shape; `gpu_info["acceleration"] == "CUDA"`/`"VAAPI"`; `gpu_info["status"] == "ok"`/`"failed"`; `card == "nvidia-0"`; `render_device is None`. Matrix systematically covered: WSL2 + CUDA available + smi confirms → ok; WSL2 + no smi → empty; container + sysfs absent + render passthrough → VAAPI; container + multi-render → 2 entries; etc.

## TestLinuxContainerNvidiaFallback

4 tests (lines 1430–1560) — all Strong. Symmetric coverage to WSL2: container with `_get_gpu_devices()==[]` + `_enumerate_nvidia_gpus_via_smi` returns GPU + CUDA test passes → NVIDIA; smi returns nothing → empty; CUDA not in hwaccels → empty; smi confirms but functional test fails → empty.

## TestDetectWindowsGPUs

5 tests (lines 1585–1684) — all Strong. NVIDIA CUDA priority over D3D11VA fallback; CUDA test fail → D3D11VA; no smi → D3D11VA; no CUDA hwaccel → D3D11VA; no hwaccels at all → []; CUDA success skips D3D11VA (early return verified by absence of WINDOWS_GPU type).

## TestProbeVulkanDevice

22 tests (lines 1700–2320) covering Vulkan probe Strategy 1→2→2b→2c→3 escalation. All Strong. Pins exact `mock_run.call_count == N` (proves which strategies fired), exact env dict (`env_arg.get("VK_DRIVER_FILES") == ...`, `env_arg.get("__EGL_VENDOR_LIBRARY_FILENAMES") == ...`), `mock_find_libegl.assert_not_called()` for gating, synthesised JSON content asserted byte-exactly against NVIDIA spec, debug buffer captures specific stderr substring, cache correctness via second-call call_count unchanged. Exemplary test design.

## TestGetVulkanDeviceInfo

5 tests (lines 2331–2374) — all Strong. is_software classification for Intel hardware (False), llvmpipe (True), lavapipe (True), None device (False); cache pinned via `mock_probe.call_count == 1` after 3 calls.

## TestGetVulkanInfoAPI

12 tests (lines 2404–2737) — all Strong. Per-case warning content asserted exhaustively:
- Case A1: NVIDIA_DRIVER_CAPABILITIES + "NVIDIA_DRIVER_CAPABILITIES=all" + GPU name
- Case A2: nvidia-container-toolkit#1041 + driver version "572.56" echoed back
- Case A3: libnvidia-glvkspirv + nvidia-container-toolkit#1559 + "legacy"
- Case A4: "diagnostic bundle" + "/api/system/vulkan/debug"
- Case B (NVIDIA + Intel + no /dri): both GPU names + "Docker Compose:" + "two paths are independent"
- Case C (Intel-only + no /dri): no NVIDIA mention + "Docker Compose:"
- Case D (Intel + /dri mapped): "already forwarded" + "vainfo" + "permissions" + no "Docker Compose:" (negative)
- Case E: "No GPU detected" + "--runtime=nvidia" + "/dev/dri"

Each test has positive + negative content pins, not just "warning present".

## TestDiagnoseVulkanEnvironment

13 tests (lines 2752–2879) — all Strong. NVIDIA_DRIVER_CAPABILITIES parsing for "all"/explicit/missing/unset; ICD path detection at /etc/ + /usr/share; libnvidia_glvkspirv glob detection; libegl_nvidia glob detection; egl_vendor_json detection; driver version parsed from /proc/driver/nvidia/version with realistic NVRM line.

## TestProbeVaapiDriver / TestFormatDriverLabel

9 tests (lines 2891–2987) — all Strong. Strict equality on parsed Driver version line; None for missing/timeout/empty; per-vendor branching pinned via `mock_probe.assert_called_once_with(path)` for Intel vs `mock_probe.assert_not_called()` for nvidia/amdgpu.

## TestIsNvidiaVulkanDevice

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2993 | `test_classification` (parametrized 13 cells) | Strong | NVIDIA brand prefix variants (RTX 4090, TITAN RTX, GeForce, Quadro, Tesla); Intel + AMD vendor prefix wins over name-based brand hint ("titan"); software rasterisers; None/empty |

## TestEnumerateNvidiaGpusViaSmi

8 tests (lines 3029–3120) — all Strong. multi-GPU CSV parse with index/name/uuid; UUID-missing tolerance; empty/rc1/FileNotFound/Timeout/RuntimeError → []; legacy `_detect_nvidia_via_nvidia_smi` returns NVIDIA/UNKNOWN.

## TestDetectLinuxGpusMultiNvidia

| Line | Test | Verdict | Note |
|---|---|---|---|
| 3131 | `test_two_nvidia_gpus_both_pass` | Strong | 2 ok entries, devices == {cuda:0, cuda:1}, uuids match, `cuda_device_index` kwarg list == ["0","1"] (pins each GPU is independently tested) |
| 3169 | `test_two_nvidia_gpus_one_fails` | Strong | One ok, silent-skip on failure |
| 3202 | `test_nvidia_via_smi_plus_amd_via_drm` | Strong | NVIDIA AND AMD both register, no duplicate |
| 3243 | `test_drm_nvidia_skipped_when_smi_primary_registered` | Strong | Dedup pinned: only cuda:0, no DRM-derived NVIDIA entry |
| 3278 | `test_single_nvidia_via_smi_regression` | Strong | exactly one cuda:0 entry |

## TestHwaccelCudaDeviceIndex

| Line | Test | Verdict | Note |
|---|---|---|---|
| 3300 | `test_cuda_index_flag_added` | Strong | Asserts `-hwaccel`+"cuda" adjacency AND `-hwaccel_device`+"1" adjacency in the actual cmd. **This is the model the TestHwaccelFunctionality success-path tests should follow.** |
| 3313 | `test_cuda_no_index_omits_flag` | Strong | `-hwaccel_device not in cmd` |

## File verdict

**File verdict: STRONG with one bug-blind cluster + 3 weak cells.** The Vulkan probe, Case-A dispatch, multi-NVIDIA detection, and WSL2/container fallback suites are exemplary. The weak spots are concentrated in older `TestHwaccelFunctionality` and `TestParseLspciGPUName` classes — both pre-date the recent audit-strengthening pass (compare with `TestHwaccelCudaDeviceIndex` at line 3300 which IS strong).

Re-review changed verdicts:
- **Strong → Bug-blind**: lines 137, 173 — `_test_hwaccel_functionality` success-path tests don't pin the FFmpeg cmd
- **Strong → Bug-blind (mild)**: lines 144, 152, 163, 206, 231, 271 — failure-path variants of same
- **Strong → Bug-blind**: line 641 `test_log_system_info` — accepts the literal label "Platform" from the SUT's format string regardless of mocked platform.system/release values
- **Strong → Weak**: lines 663, 674, 685 — `TestParseLspciGPUName` tests only the fallback branch (rc=1), never the actual lspci-parsing path
- **Already Weak (acceptable)**: lines 343, 353 — fallback string substrings, free-form contract

## Fix queue

| Line | Test | Fix |
|---|---|---|
| 137 | `test_test_hwaccel_functionality_cuda_success` | After asserting `result is True`, add: `cmd = mock_run.call_args.args[0]`; `assert cmd[0] == "ffmpeg"`; `assert "-hwaccel" in cmd and cmd[cmd.index("-hwaccel")+1] == "cuda"`; `assert "-i" in cmd`. Use `TestHwaccelCudaDeviceIndex.test_cuda_index_flag_added` (line 3300) as the model. |
| 173 | `test_test_hwaccel_functionality_vaapi_success` | After `result is True`, add: `cmd = mock_run.call_args.args[0]`; `assert "-hwaccel" in cmd and cmd[cmd.index("-hwaccel")+1] == "vaapi"`; `assert "-vaapi_device" in cmd and cmd[cmd.index("-vaapi_device")+1] == "/dev/dri/renderD128"`. |
| 144, 152, 163, 206, 231, 271 | failure-path hwaccel tests | Lower-priority: optionally pin cmd structure on the fail path so a regression that builds the wrong cmd, fails for the wrong reason, and happens to return False isn't masked. |
| 641 | `test_log_system_info` | Replace the OR-disjunction. Either: (a) mock `platform.platform()` (the actual SUT call) and assert that mocked value flows into the debug call; or (b) assert that at least one debug call's *formatted* output contains a value that could only have come from the mock (e.g., capture `mock_logger.debug.call_args_list`, render each call as `args[0].format(*args[1:])`, and substring-match against the mocked platform string). The current "Platform" / "Kernel" literals from the format string make the test pass even when the mocks are wired to the wrong attribute (which they currently are: `platform.system`/`platform.release` are mocked but the SUT calls `platform.platform()`/`platform.python_version()`). |
| 663, 674, 685 | `TestParseLspciGPUName.*` | These tests claim to test "parsing" but exercise only the fallback branch. Add at least one positive parse test per vendor: mock `subprocess.run` with realistic lspci stdout (`"01:00.0 VGA compatible controller: Advanced Micro Devices [AMD/ATI] Radeon RX 6800"`) and assert the returned name contains the parsed substring (e.g. "Radeon" or "AMD/ATI"). The existing fallback tests can stay alongside, just rename to `..._fallback_when_lspci_fails`. |
