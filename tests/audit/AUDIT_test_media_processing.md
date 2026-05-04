# Audit: tests/test_media_processing.py — adversarial re-review

3,972 lines, ~137 tests, 26 classes. The previous single-pass audit verdicted the file STRONG. This re-review is adversarial: assume the previous verdict missed something. Particular focus on the patterns:
1. tautological subprocess.run mocks (mock returns text, SUT returns same text)
2. bug-blind subprocess assertions (call happened but cmd contents not pinned)
3. `assert "X" in vf` substring checks where filter ORDERING matters
4. bug-locking on FFmpeg flag spelling
5. matrix gaps (HDR10 / HLG / DolbyVision / SDR; Intel/AMD/NVIDIA/Apple)

Production SUT references:
- `media_preview_generator/processing/generator.py` (parse_ffmpeg_progress_line, _detect_codec_error, _detect_hwaccel_runtime_error, generate_bif at L1223, _diagnose_ffmpeg_exit_code at L309)
- `media_preview_generator/processing/ffmpeg_runner.py`
- `media_preview_generator/processing/hdr_detection.py`

## TestBIFGeneration

| Line | Test | Verdict | Note |
|---|---|---|---|
| 45 | `test_generate_bif_creates_valid_structure` | Strong | magic[8] exact, version, image_count, frame_interval — all pinned with `==` |
| 78 | `test_generate_bif_index_table` | Strong | per-entry timestamp+offset arithmetic, end marker `0xFFFFFFFF` pinned |
| 113 | `test_generate_bif_embedded_images` | Strong | unique sentinel substring catches mojibake / truncation |
| 130 | `test_generate_bif_empty_directory` | Strong | image_count==0 |
| 141 | `test_generate_bif_frame_interval` | Strong | strict 10000 ms |

## TestFFmpegProgressParsing

| Line | Test | Verdict | Note |
|---|---|---|---|
| 162 | `test_parse_ffmpeg_progress_line_duration` | Strong | arithmetic 3600+23*60+45.67 with tolerance |
| 171 | `test_parse_ffmpeg_progress_line_progress` | Strong | callback receives frame=1234, fps≈45.6, speed=="1.23x", time_str=="00:12:34.56" — multiple distinct fields |
| 207 | `test_parse_ffmpeg_progress_line_with_callback` | Strong | callback fired |
| 220 | `test_parse_ffmpeg_progress_line_progress_decimal_precision` | Strong | progress==33.3 (round to 1dp) AND `isinstance(.., float)` |
| 246 | `test_parse_ffmpeg_progress_line_no_callback` | Strong | returns total_duration with callback=None |
| 252 | `test_remaining_time_accounts_for_speed` | Strong | wallclock = remaining/speed → 3.0 ± 0.1 with arithmetic |
| 281 | `test_remaining_time_at_1x_speed` | Strong | 90s identity |
| 305 | `test_remaining_time_no_speed_falls_back` | Strong | speed unparseable → falls back to raw (90s) |

## TestDetectCodecError

| Line | Test | Verdict | Note |
|---|---|---|---|
| 333 | `test_detect_codec_error_stderr_patterns` | Strong | 6 distinct phrasings, all → True |
| 349 | `test_detect_codec_error_exit_code_69` | Strong | |
| 355 | `test_detect_codec_error_exit_code_minus22` | Strong | EINVAL branch |
| 361 | `test_detect_codec_error_exit_code_234` | Strong | wrapped -22 on Unix |
| 367 | `test_detect_codec_error_no_match` | Strong | negative |
| 373 | `test_detect_codec_error_success_exit` | Strong | stderr precedence over rc=0 |
| 380 | `test_detect_codec_error_success_exit_no_codec_error` | Strong | full negative |
| 386 | `test_detect_codec_error_case_insensitive` | Strong | 3 case variants |

## TestDetectHwaccelRuntimeError

10 tests (lines 397–453) — Strong. Each pins detection of one specific stderr pattern (VAAPI surface sync, transfer data, AVHWFramesContext, CUDA, cuvid, hwaccel init, surface creation), plus negative + empty/None + case-insensitive.

## TestGenerateImages

| Line | Test | Verdict | Note |
|---|---|---|---|
| 465 | `test_generate_images_calls_ffmpeg` | Strong | argv[0] match, `-i` followed by exact source path, output under temp_dir. Comment cites the prior weak `_path in args` form. |
| 521 | `test_ffmpeg_stall_timeout_kills_process` | Strong | success is False, kill+wait call_count == 2 (two runs both hit stall) |
| 581 | `test_generate_images_gpu_nvidia` | Strong | `-hwaccel cuda` adjacency pinned, no `-hwaccel_device` for generic cuda, source still wired through `-i` |
| 632 | `test_generate_images_gpu_nvidia_indexed_device` | Strong | cuda:1 → `-hwaccel_device 1` pinned exactly |
| 673 | `test_generate_images_gpu_amd` | **Weak** — see "Why downgraded" | Asserts `"vaapi" in args` and `"/dev/dri/renderD128" in args` independently — no adjacency to `-hwaccel`. A regression that drops `-hwaccel` while keeping `vaapi` somewhere else (e.g. `vaapi_device`) would still pass. **Why downgraded:** the NVIDIA equivalent (line 581) was already strengthened to use `args[args.index("-hwaccel") + 1] == "cuda"`; the AMD path needs the same treatment to catch token-reorder regressions. |
| 714 | `test_generate_images_cpu_only` | Strong | iterates all `-hwaccel` occurrences and asserts the value is not in the GPU-decoder set |
| 763 | `test_generate_images_hdr_detection` | Strong | zscale-before-tonemap order pinned with `find()` index comparison; `tonemap=` algorithm presence pinned |
| 824 | `test_generate_images_renames_files` | Strong | exact rename target list |
| 870 | `test_generate_images_accepts_progress_callback_signature` | Strong | inspect.signature pin on positional+name+kind |
| 904 | `test_generate_images_raises_codec_error_in_gpu_context` | Strong | raises + msg substring + popen.call_count==2 + cleanup verified |
| 975 | `test_generate_images_no_cpu_fallback_when_disabled` | Strong | raises + popen.call_count==2 |
| 1045 | `test_generate_images_no_cpu_fallback_when_no_codec_error` | Strong | success is False, image_count==0, popen.call_count==2 |
| 1118 | `test_generate_images_dolby_vision_rpu_error_retries_with_dv_safe_filter_on_gpu` | Strong | popen.call_count==3, third arg checked for scale_cuda+hwdownload, no zscale/tonemap |
| 1220 | `test_generate_images_dolby_vision_rpu_error_cpu_returns_failure` | Strong | full tuple destructure (success, count, hw_used) checked + popen.call_count==3 |
| 1284 | `test_generate_images_dolby_vision_rpu_error_retries_with_dv_safe_filter_on_cpu` | Strong | third call vf checked uses CPU `scale=` not `scale_cuda=` |

## TestMediaInfoImport

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1368 | `test_mediainfo_can_parse` | Framework-trivia | Tests the pymediainfo library, not our code. `assert result is True or result is False` is a tautology (any boolean satisfies). Safe to delete or keep as a smoke import check. |

## TestDVNoBackwardCompat

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1380 | `test_detection` (parametrized 14 cells) | Strong | DV Profile 5 (HEVC + AV1), DV Profile 4 (with/without misleading 'compatible'), DV Profile 7/8 (HDR10 / HLG compat), plain HDR10, None/empty — exhaustive matrix, each cell asserts `is True/False` strictly |

## TestIsDolbyVision

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1423 | `test_detection` (parametrized 11 cells) | Strong | All DV profile variants → True; plain HDR10/HLG → False; None/empty → False |

## TestDetectZscaleColorspaceError

6 tests (lines 1461–1495) — Strong. Pins detection of bracketed (`[Parsed_zscale_1 @ ...]`, `[vf#0:0/zscale @ ...]`) and bare `zscale:` patterns, negative for unrelated errors, empty/None.

## TestProactiveDVSkip

13 tests (lines 1596–2235) using shared `_run_generate` helper.

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1596 | `test_generate_images_dv_profile5_uses_libplacebo` (Intel OpenCL) | **Bug-locking** — see "Why downgraded" | Pins `args[init_indices[0] + 1] == "vaapi=va:/dev/dri/renderD128"` and `"opencl=ocl@va"` — exact FFmpeg flag spelling for a third-party patch (jellyfin-ffmpeg's DV-aware tonemap_opencl). Also pins `"hwmap=derive_device=opencl:mode=read"` exactly. **Why downgraded:** if jellyfin-ffmpeg or upstream FFmpeg renames any of these tokens (e.g. `mode=read` becomes `mode=ro`), the test fails even though the SUT still produces a correct-by-construction chain. The fps-before-hwmap ordering (line 1683) IS correctly tested; that's the regression-worthy assertion. The exact device-init string is over-pinned. **Acceptable risk** because the project documents it pins jellyfin-ffmpeg behaviour deliberately — flagged for awareness. |
| 1697 | `test_generate_images_dv_profile5_amd_uses_vaapi_hwaccel` | Bug-locking (mild) | Same concern: pins `drm=dr:/dev/dri/renderD129`, `vaapi=va@dr`, `vulkan=vk@dr` exactly. The `init_indices` count==3 plus per-index value pinning. Intentional contract pin — acceptable. |
| 1762 | `test_generate_images_dv_profile5_non_dri_falls_back_to_sw_decode` | Strong | Negative + positive: no `-hwaccel`, only `vulkan=vk` device init, `-threads:v 0` for SW decode |
| 1827 | `test_generate_images_dv_profile5_software_vulkan_uses_dv_safe_filter` | Strong | Uses fixture override; vf.startswith("fps=fps=") |
| 1903 | `test_generate_images_dv_profile5_no_vulkan_device_uses_dv_safe_filter` | Strong | Same guard with device=None |
| 1960 | `test_generate_images_dv_profile5_nvidia_uses_nvdec` | Strong | -hwaccel cuda BEFORE -i, libplacebo retained, -threads:v 1 |
| 2038 | `test_generate_images_dv_profile5_cpu_skips_hwaccel` | Strong | No -hwaccel, no -threads:v, libplacebo still present |
| 2089 | `test_generate_images_dv_profile8_hdr10_uses_zscale` | Strong | zscale + tonemap, no libplacebo |
| 2139 | `test_generate_images_dv_hdr10plus_uses_zscale` | Strong | DV+HDR10+ → HDR10 base layer routing |
| 2188 | `test_dv_profile8_with_gpu_uses_cuda_and_zscale` | Strong | NVIDIA + DV8 |

## TestLibplaceboFallback

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2251 | `test_generate_images_dv_profile5_libplacebo_failure_falls_back` | Strong | First call has libplacebo + vulkan; second call has neither — both branches checked |

## TestZscaleErrorRetry

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2359 | `test_generate_images_zscale_error_triggers_dv_safe_retry` | Strong | popen.call_count==3, third call has scale_cuda+hwdownload, no zscale/tonemap |

## TestDVSafeRetryGpuFailure

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2457 | `test_generate_images_dv_safe_retry_gpu_failure_raises_codec_error` | Strong | All 3 attempts fail → CodecNotSupportedError; popen.call_count==3 |

## TestDynamicNpl

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2522 | `test_npl_always_100_with_maxcll` | Strong | `npl=100` in vf, `npl=1000` NOT in vf (positive + negative) |
| 2587 | `test_npl_always_100_without_maxcll` | Strong | |
| 2651 | `test_hdr_uses_desat_0` | Strong | `desat=0` + `npl=100` |

## TestDetectDolbyVisionRPUError

5 tests (lines 2710–2728) — Strong.

## TestSaveFFmpegFailureLog

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2734 | `test_creates_log_file` | Strong | exit_code, signal_killed, exit_diagnosis, error lines all asserted via substring |
| 2753 | `test_caps_at_500_files` | Strong | `len <= 501` |
| 2773 | `test_handles_oserror_gracefully` | Strong | Implicit "no raise" |

## TestDiagnoseFFmpegExitCode

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2796 | `test_classifies_exit_codes` (parametrized 8) | Strong | Covers signals (130/137/143), I/O (251), high-non-signal (187), negative (-15), success (0), generic (1) |

## TestVerifyTmpFolderHealth

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2803 | `test_returns_healthy_for_writable_directory` | Strong | (True, []) strict |
| 2808 | `test_returns_error_for_unwritable_directory` | Strong | substring + is False |
| 2815 | `test_warns_when_disk_space_low` | Strong | substring on warning text |

## TestHdrFormatNoneString

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2837 | `test_hdr_format_none_string_uses_sdr_path` | Strong | Negative (no zscale/tonemap) + positive (fps, scale) |

## TestGpuScaleOptimisation (issue #218)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2918 | `test_nvidia_sdr_uses_scale_cuda_with_hwaccel_output_format` | Strong | scale_cuda + format=nv12 + hwdownload + force_divisible_by=2; explicit anti-regression `assert "scale=w=320" not in vf.replace(...)` |
| 2944 | `test_nvidia_hdr10_downscales_on_gpu_before_zscale` | Strong | scale_cuda BEFORE zscale via index comparison; format=p010le pinned |
| 2965 | `test_vaapi_sdr_uses_scale_vaapi_with_even_parity_safety` | Strong | scale_vaapi + parity-fix scale=trunc(iw/2)*2; explicit `-vaapi_device not in args` (deprecated form rejected) |
| 2992 | `test_vaapi_hdr10_downscales_on_gpu_before_zscale` | Strong | scale_vaapi < parity_idx < zscale_idx ordering chain |
| 3013 | `test_cpu_path_retains_software_scale` | Strong | Negative (no scale_cuda/scale_vaapi/hwdownload, no -hwaccel_output_format) + positive |
| 3041 | `test_dv5_libplacebo_vf_unchanged` | Strong | fps-first BEFORE hwupload via index comparison; no `:fps=` inside libplacebo (regression guard); -hwaccel cuda set but `-hwaccel_output_format` NOT set |

## TestFfmpegThreadFlags

| Line | Test | Verdict | Note |
|---|---|---|---|
| 3082 | `test_gpu_path_includes_thread_cap` | Strong | -threads 2, -filter_threads 2, -threads:v 1 — all three pinned with adjacency |
| 3129 | `test_cpu_path_omits_thread_cap` | Strong | Issue #212 regression guard; explicit count of bare -threads occurrences |
| 3171 | `test_gpu_path_zero_threads_omits_cap` | Strong | bare -threads occurrence count == 0 |

## TestCancellation

| Line | Test | Verdict | Note |
|---|---|---|---|
| 3212 | `test_cancel_kills_ffmpeg_process` | Strong | raises + terminate.assert_called_once |
| 3256 | `test_cancel_skips_skip_frame_retry` | Strong | popen.call_count==1 |
| 3310 | `test_cancel_skips_dv_safe_retry` | Strong | popen.call_count==1 even with DV hdr_format |
| 3365 | `test_cancel_skips_gpu_to_cpu_fallback` | Strong | popen.call_count==2 |
| 3424 | `test_cancel_after_pause_resumes_before_terminating` | Strong | SIGCONT-before-terminate pinned via `method_calls` index ordering — exemplary |
| 3509 | `test_cancel_falls_back_to_kill_when_terminate_times_out` | Strong | terminate + kill.assert_called_once + wait>=2 |

## TestFailureScope

5 tests (lines 3573–3667) — Strong. Real threading + Event sync; per-job isolation pinned; nested same-job sharing verified; clear_failures only drops current scope.

## TestSkipFrameInitialDefaults

| Line | Test | Verdict | Note |
|---|---|---|---|
| 3730 | `test_sdr_first_attempt_uses_skip_frame` | Strong | `-skip_frame:v` followed by `nokey` (exact) |
| 3777 | `test_dv_profile8_hdr10_first_attempt_uses_skip_frame` | Strong | DV8 + HDR10 still uses skip_frame |
| 3829 | `test_retry_drops_skip_frame_when_first_attempt_fails` | Strong | First call has flag, retry doesn't |

## TestBuildDV5Vf

| Line | Test | Verdict | Note |
|---|---|---|---|
| 3898 | `test_intel_opencl_chain_is_byte_identical` | Strong | Full byte-exact equality on the assembled vf string |
| 3916 | `test_vaapi_vulkan_chain_is_byte_identical` | Strong | Same |
| 3932 | `test_libplacebo_hwupload_chain_is_byte_identical` | Strong | Same |
| 3948 | `test_unknown_path_kind_raises` | Strong | ValueError + match |
| 3957 | `test_fps_appears_first_in_every_variant` | Strong | startswith pinned across all 3 path kinds |

## File verdict

**File verdict: STRONG with one weak cell + one acknowledged framework-trivia.** 1 `Weak` (downgraded from Strong by re-review) and 2 `Bug-locking-mild` items flagged for awareness, 1 `Framework-trivia`. The vast majority remain Strong on second look — the test file is genuinely high quality and the first-pass audit was substantially correct.

Re-review changed verdicts:
- **Strong → Weak**: line 673 `test_generate_images_gpu_amd` (no adjacency check on `-hwaccel vaapi`)
- **Strong → Bug-locking (mild, intentional)**: lines 1596 + 1697 — exact pinning of jellyfin-ffmpeg-specific device-init strings (`vaapi=va:/dev/dri/renderD128`, `opencl=ocl@va`, `drm=dr:/dev/dri/renderD129`, `vaapi=va@dr`, `vulkan=vk@dr`). Acceptable contract-pinning; flagged so a future jellyfin-ffmpeg rev doesn't surprise us.
- **Already Framework-trivia**: line 1368 `test_mediainfo_can_parse`.

## Fix queue

| Line | Test | Fix |
|---|---|---|
| 673 | `test_generate_images_gpu_amd` | Add `assert args[args.index("-hwaccel") + 1] == "vaapi"` and `assert args[args.index("-hwaccel_device") + 1] == "/dev/dri/renderD128"` (or equivalent for whichever flag carries the device); follow the same shape as the NVIDIA test at line 581. Without this, a token-reorder regression that drops `-hwaccel` but leaves `vaapi` floating in argv would silently pass. |
| 1368 | `test_mediainfo_can_parse` | Either delete or replace the tautological `result is True or result is False` with a real smoke check (e.g. `assert MediaInfo.can_parse() in (True, False)` is identical — replace with an actual parse of a known fixture, or just `import` and call `MediaInfo()`). Optional — not a regression risk. |
| 1596, 1697 | Intel/AMD DV5 device-init pinning | No code fix required. Add a comment noting these strings track jellyfin-ffmpeg's contract verbatim and must be re-evaluated if jellyfin-ffmpeg renames init flags. |
