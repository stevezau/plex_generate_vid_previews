# Audit: tests/test_media_processing.py — ~137 tests, 25 classes

## TestBIFGeneration

| Line | Test | Verdict |
|---|---|---|
| 45 | `test_generate_bif_creates_valid_structure` | **Strong** — magic bytes (exact 8-byte list), version, image_count==3, frame_interval==5000 |
| 78 | `test_generate_bif_index_table` | **Strong** — per-entry timestamp+offset arithmetic, end marker `0xFFFFFFFF` pinned |
| 113 | `test_generate_bif_embedded_images` | **Strong** — substring on a unique sentinel (`b"TEST_IMAGE_DATA_12345"`) — appropriate |
| 130 | `test_generate_bif_empty_directory` | **Strong** — image_count==0 |
| 141 | `test_generate_bif_frame_interval` | **Strong** — strict 10000 ms |

## TestFFmpegProgressParsing

All 8 tests (lines 162–327) — **Strong**: each pins computed values (frame, fps with tolerance, `time_str` exact equality, `speed=="100.0x"`, remaining-time wallclock vs raw with arithmetic).

## TestDetectCodecError

All 8 tests (lines 333–391) — **Strong**: positive sweeps of stderr patterns, negative cases, exit-code branches (69, -22, 234, 0), case-insensitivity. Each asserts `is True`/`is False` strictly.

## TestDetectHwaccelRuntimeError

All 10 tests (lines 397–453) — **Strong**: each pins detection of a specific stderr line type (VAAPI surface sync, transfer data, AVHWFramesContext, CUDA, cuvid, hwaccel init), negative cases for empty/None.

## TestGenerateImages

| Line | Test | Verdict |
|---|---|---|
| 465 | `test_generate_images_calls_ffmpeg` | **Strong** — argv[0] exact match, `-i` followed by exact source path, output under temp_dir. The audit comment explicitly addresses the prior "_path in args" weakness. |
| 521 | `test_ffmpeg_stall_timeout_kills_process` | **Strong** — `success is False`, kill+wait call_count == 2 (proves both runs hit stall) |
| 581 | `test_generate_images_gpu_nvidia` | **Strong** — `-hwaccel cuda` adjacency pinned (audit-fixed comment cites the prior weak form), no `-hwaccel_device` for generic cuda |
| 632 | `test_generate_images_gpu_nvidia_indexed_device` | **Strong** — cuda:1 → `-hwaccel_device 1` pinned exactly |
| 673 | `test_generate_images_gpu_amd` | **Strong** — vaapi + device path |
| 714 | `test_generate_images_cpu_only` | **Strong** — explicit set-membership negative check across all 7 GPU decoder names |
| 763 | `test_generate_images_hdr_detection` | **Strong** — zscale-before-tonemap order pinned with index comparison; `tonemap=` algorithm presence pinned |
| 824 | `test_generate_images_renames_files` | **Strong** — exact rename target list `["0000000000.jpg", "0000000005.jpg", "0000000010.jpg"]` (audit-fixed) |
| 870 | `test_generate_images_accepts_progress_callback_signature` | **Strong** — uses `inspect.signature` to pin position+name+kind of `progress_callback` parameter (audit replaced a no-assert test) |
| 904 | `test_generate_images_raises_codec_error_in_gpu_context` | **Strong** — `pytest.raises(CodecNotSupportedError)` + message substring + popen call_count==2 |
| 975 | `test_generate_images_no_cpu_fallback_when_disabled` | **Strong** — raises + popen.call_count==2 |
| 1045 | `test_generate_images_no_cpu_fallback_when_no_codec_error` | **Strong** — `success is False`, image_count==0, popen.call_count==2 |
| 1118 | `test_generate_images_dolby_vision_rpu_error_retries_with_dv_safe_filter_on_gpu` | **Strong** — popen.call_count==3, third arg checked for scale_cuda+hwdownload, no zscale/tonemap |
| 1220 | `test_generate_images_dolby_vision_rpu_error_cpu_returns_failure` | **Strong** — full tuple unpack (success, count, hw_used) checked + popen.call_count==3 |
| 1284 | `test_generate_images_dolby_vision_rpu_error_retries_with_dv_safe_filter_on_cpu` | **Strong** — third call vf checked: `scale=` not `scale_cuda=` (CPU path) |

## TestMediaInfoImport

| Line | Test | Verdict |
|---|---|---|
| 1368 | `test_mediainfo_can_parse` | **Framework trivia** — tests the pymediainfo library, not our code. `assert result is True or result is False` is a tautology. **Verdict: keep but acknowledge — it's a smoke check that the library is installed and importable.** |

## TestDVNoBackwardCompat / TestIsDolbyVision

All parametrized (lines 1380, 1423) — **Strong**: 14+11 cells each, every cell asserts `is True` / `is False` on the boolean output.

## TestDetectZscaleColorspaceError

All 6 tests (lines 1461–1495) — **Strong**: each pins detection of bracketed/non-bracketed zscale error patterns; negative for unrelated.

## TestProactiveDVSkip

13+ tests (lines 1596–2235) using shared `_run_generate` helper — **Strong**: each tests a specific DV5 routing matrix cell:
- Intel VAAPI+OpenCL chain (init devices, hwaccel device, vf filter chain pinned with `assert vf.find("X") < vf.find("Y")` for ordering)
- AMD VAAPI+Vulkan libplacebo
- Non-DRI software fallback
- Software Vulkan / no Vulkan device → DV-safe filter
- NVIDIA NVDEC + libplacebo
- DV Profile 8 + HDR10 → zscale (no libplacebo)
- DV+HDR10+ → zscale

Each asserts exact filter substrings AND ordering — not just presence.

## TestLibplaceboFallback / TestZscaleErrorRetry / TestDVSafeRetryGpuFailure

All **Strong** (lines 2251–2506): specific scenario walkthroughs, exact `mock_popen.call_count`, third-call vf inspection, `pytest.raises(CodecNotSupportedError)`.

## TestDynamicNpl

| Line | Test | Verdict |
|---|---|---|
| 2522 | `test_npl_always_100_with_maxcll` | **Strong** — `npl=100` in vf, `npl=1000` NOT in vf (positive + negative pin) |
| 2587 | `test_npl_always_100_without_maxcll` | **Strong** |
| 2651 | `test_hdr_uses_desat_0` | **Strong** — `desat=0` in vf |

## TestDetectDolbyVisionRPUError

All 5 tests (lines 2710–2728) — **Strong**: positive + case-insensitive + negative + empty.

## TestSaveFFmpegFailureLog / TestDiagnoseFFmpegExitCode / TestVerifyTmpFolderHealth

| Line | Test | Verdict |
|---|---|---|
| 2734 | `test_creates_log_file` | **Strong** — content substrings: file, exit_code, signal_killed, exit_diagnosis, error lines |
| 2753 | `test_caps_at_500_files` | **Strong** — `len(log_files) <= 501` |
| 2773 | `test_handles_oserror_gracefully` | **Strong** — "should not raise" — pytest catches, no explicit assert needed |
| 2796 | `test_classifies_exit_codes` | **Strong** — parametrized 8-cell matrix |
| 2803 | `test_returns_healthy_for_writable_directory` | **Strong** — `(True, [])` strict |
| 2808 | `test_returns_error_for_unwritable_directory` | **Strong** — `not writable` substring + `is False` |
| 2815 | `test_warns_when_disk_space_low` | **Strong** — `low free space` substring |

## TestHdrFormatNoneString

| Line | Test | Verdict |
|---|---|---|
| 2837 | `test_hdr_format_none_string_uses_sdr_path` | **Strong** — negative checks (zscale not in, tonemap not in) + positive (fps, scale) |

## TestGpuScaleOptimisation (issue #218)

All 6 tests (lines 2918–3069) — **Strong**: each pins specific GPU-scale chain ordering (`scale_cuda` before `zscale`, parity-fix `scale=trunc(iw/2)*2`, `format=p010le` for HDR), specific output formats (`-hwaccel_output_format cuda`/`vaapi`), and counter-checks (`scale=w=320` not in vf when scale_cuda used). The DV5 libplacebo test asserts fps-first ordering with `vf.index("fps=fps=") < vf.index("hwupload,")`.

## TestFfmpegThreadFlags

All 3 tests (lines 3082–3200) — **Strong**: GPU path includes -threads + -filter_threads + -threads:v 1 (oversubscription guard); CPU path explicitly omits all (regression issue #212); zero-threads omits -threads.

## TestCancellation

All 6 tests (lines 3212–3563) — **Strong**: `pytest.raises(CancellationError)` plus secondary contract assertions (terminate.assert_called_once, popen call_count limited, SIGCONT-before-terminate ordering verified by index comparison, kill escalation after terminate timeout). Excellent regression guards.

## TestFailureScope

All 5 tests (lines 3573–3667) — **Strong**: per-job scope isolation pinned via real threads + Event sync; nested-same-job sharing verified.

## TestSkipFrameInitialDefaults

All 3 tests (lines 3730–3886) — **Strong**: `-skip_frame:v` followed by `nokey` (exact); retry path drops the flag.

## TestBuildDV5Vf

All 5 tests (lines 3898–3972) — **Strong**: byte-identical equality on full vf string for each path kind; ValueError raises with match; fps-first verified across all 3 variants.

## Summary

- **~137 tests**
- **~136 Strong**, **1 Framework trivia** (test_mediainfo_can_parse line 1368)
- The DV5 routing tests are exemplary — they assert exact filter substrings AND argument ordering with `vf.find()` index comparisons, catching the entire class of "filter chain rearranged" regressions
- TestGenerateImages was clearly recently audit-strengthened: comments explicitly cite prior weak forms ("would have passed silently"), and assertions now check argument adjacency (e.g. `args[args.index("-hwaccel") + 1] == "cuda"` rather than `"cuda" in args`)
- 0 bug-blind / tautological / bug-locking finds requiring fix

**File verdict: STRONG.** Optional: line 1368 `test_mediainfo_can_parse` is framework trivia — keep as a smoke check or delete; not material.
