# Audit: tests/test_windows_compatibility.py — 9 tests, 5 classes

## TestWindowsPlatformDetection

| Line | Test | Verdict |
|---|---|---|
| 32 | `test_is_windows_on_windows` | **Strong** — `@patch("os.name", "nt")` + `is True`. Pins the detection helper on the Windows cell. |
| 37 | `test_is_windows_on_linux` | **Strong** — mirror cell (posix → False). Together fully cover the helper's truth table. |

## TestWindowsPathSanitization

| Line | Test | Verdict |
|---|---|---|
| 46 | `test_sanitize_path_forward_to_backslash` | **Strong** — strict equality `result == "C:\\Users\\Test\\Videos\\movie.mkv"` AND defensive `"/" not in result`. Two-anchor on slash translation. |
| 55 | `test_sanitize_path_unc_path` | **Strong** — `startswith("\\\\")` + strict equality on the full `\\\\server\\share\\…` form. Pins UNC handling specifically. |
| 63 | `test_sanitize_path_already_backslash` | **Strong** — idempotency cell: already-backslashed input passes through unchanged. |
| 70 | `test_sanitize_path_normpath_uses_ntpath` | **Strong (audit-aware)** — patches `os.path` to point at the real `ntpath` module rather than mocking normpath itself, then asserts the full real Windows pipeline (slash conversion + `..` collapse + `.` collapse) yields `"C:\\Users\\Videos\\movie.mkv"`. The docstring explicitly contrasts with mocking-its-own-mock. |
| 86 | `test_sanitize_path_linux_unchanged` | **Strong** — equality on POSIX path AND `"\\" not in result`. Two-anchor; second guards against a regression that emits backslashes on POSIX. |

## TestWindowsTempDirectory

| Line | Test | Verdict |
|---|---|---|
| 114 | `test_windows_default_temp_folder` | **Strong-ish** — heavy mock setup (10 patches), but asserts `config.tmp_folder == "C:\\Temp"` exactly. The mock load is large enough that "this exercises load_config under Windows env" is the real claim — the assertion at the end is specific. **Minor concern**: with 10 mocks the test boundary is fuzzy; if `tempfile.gettempdir` were no longer called, the test could falsely pass via direct env value. But the patch return is unique enough that the equality is meaningful. |

## TestWindowsPathMappings

| Line | Test | Verdict |
|---|---|---|
| 188 | `test_path_mapping_windows_to_windows` | **Strong** — calls real `path_to_canonical_local`, asserts intermediate `canonical == "C:/Media/Movies/movie.mkv"` AND final `sanitize_path(canonical) == "C:\\Media\\Movies\\movie.mkv"`. Two equalities exercise both stages. |
| 209 | `test_path_mapping_unc_to_local` | **Strong** — mirror cell with UNC → local Windows. Same two-anchor pattern. |

## TestWindowsConfigValidation

| Line | Test | Verdict |
|---|---|---|
| 243 | `test_windows_config_validation` | **Strong-ish** — same heavy-mock pattern as line 114. Asserts `gpu_threads == 0` AND `cpu_threads == 4`. The two integer checks pin the env→config flow; not bug-blind. **Minor concern**: nearly identical setup to line 114 — these two could share a fixture. Worth noting as DRY-ness rather than correctness. |

## Summary

- **9 tests** — 7 Strong, 2 Strong-ish (heavy mocks but specific assertions)
- 0 bug-blind, 0 tautological, 0 dead/redundant, 0 bug-locking, 0 needs-human
- Sanitization matrix: forward→back / UNC / idempotent / normpath-with-real-ntpath / POSIX-no-op — comprehensive
- Path-mapping covers both Windows-to-Windows and UNC-to-Windows shapes
- Two heavy load_config tests (lines 114, 243) are nearly duplicates by setup but assert different fields — not redundant by content

**File verdict: STRONG.** Two cosmetic notes:
1. Lines 114 and 243 share massive mock fixtures that could be consolidated (DRY, not correctness).
2. The `test_sanitize_path_normpath_uses_ntpath` test (line 70) is exemplary — it explicitly avoids the "mock the function under test" tautology trap.

No fix-blocking issues.
