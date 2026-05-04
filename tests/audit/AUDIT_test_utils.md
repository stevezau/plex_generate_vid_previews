# Audit: tests/test_utils.py — 36 tests, 8 classes

## TestCalculateTitleWidth

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 29 | `test_calculate_title_width` | **Weak** | Asserts only `20 <= width <= 50` — a range bound. A regression returning the constant `35` (or any value in range) would pass. Worth pinning the actual value the formula produces for cols=120. |
| 41 | `test_calculate_title_width_small_terminal` | **Weak** | Only `width >= 20` — minimum-floor check. A regression returning a hardcoded `20` always would pass. |
| 53 | `test_calculate_title_width_large_terminal` | **Weak** | Only `width <= 50` — maximum-ceiling check. Same problem; a hardcoded `50` always would pass. |

## TestFormatDisplayTitle

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 68 | `test_format_display_title_episode_short` | **Strong** | Substring check + strict `len(result) == 30` padding contract. |
| 79 | `test_format_display_title_episode_long` | **Strong** | Asserts `S01E01` preserved AND ellipsis appears AND length cap. Catches truncation bugs. |
| 91 | `test_format_display_title_movie` | **Weak** | The OR clause `"Shawshank" in result or title in result` is permissive — the second branch matches any non-truncated form. Padding length check (`== 30`) is the load-bearing assertion. |
| 101 | `test_format_display_title_movie_long` | **Strong** | Ellipsis present + length cap. |
| 111 | `test_format_display_title_preserves_season_episode` | **Strong** | Single substring but it IS the contract — S05E12 must survive truncation. |

## TestSanitizePath

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 124 | `test_sanitize_path_windows` | **Strong** | Both positive (`"\\" in result`) AND negative (`"/" not in result`). |
| 134 | `test_sanitize_path_unix` | **Strong** | Strict equality `result == path` plus negative on backslashes. |
| 147 | `test_sanitize_path_unix_normalises_redundant_separators` | **Strong** | Two strict equality checks — `//` and `..` collapse. Tests REAL `normpath` (not mocked), catches a no-op replacement. |
| 156 | `test_sanitize_path_windows_mixed` | **Strong** | Positive + negative on slash conversion. |

## TestSafeResolveWithin

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 184 | `test_null_byte_injection_returns_none` | **Strong** | Pins null-byte short-circuit (CWE-158 boundary). |
| 190 | `test_dotdot_traversal_rejected` | **Strong** | Pins `..` traversal rejection. |
| 197 | `test_absolute_path_outside_root_rejected` | **Strong** | Pins absolute-outside rejection. |
| 203 | `test_symlink_escape_rejected` | **Strong** | Pins symlink rejection AFTER realpath — important: the link itself is inside root. |
| 219 | `test_path_inside_root_allowed` | **Strong** | Strict equality on resolved realpath. |
| 230 | `test_exact_root_match_allowed` | **Weak** | Only `is not None`. A regression returning a wrong path would pass. Should equality-check the realpath. |
| 236 | `test_root_equals_filesystem_root_allows_anything` | **Strong** | Documented contract — `result is None or result.startswith("/")`. The "didn't reject SOLELY because of /" test. |
| 248 | `test_relative_path_normalised_before_check` | **Strong** | `is not None` but in context: the explicit failure mode is rejection, so `is not None` IS the contract here. Acceptable. |
| 264 | `test_prefix_confusion_with_sibling_path_rejected` | **Strong** | Pins the os.sep separator guard against `/data` vs `/data-attacker` prefix confusion. Critical security boundary. |
| 290 | `test_nonexistent_path_inside_root_still_resolves` | **Strong** | Pins not-yet-existing-path passing — needed for fresh-download webhook. Both `is not None` AND substring containment. |

## TestIsWindows

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 317 | `test_is_windows_on_windows` | **Strong** | Strict `is True`. |
| 322 | `test_is_windows_on_posix` | **Strong** | Strict `is False`. |

## TestIsDockerEnvironment

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 333 | `test_is_docker_environment_dockerenv` | **Strong** | `is True` with the `/.dockerenv` discriminator. |
| 341 | `test_is_docker_environment_container_env` | **Strong** | `is True` via `container=docker` env. |
| 350 | `test_is_docker_environment_docker_container_env` | **Strong** | `is True` via `DOCKER_CONTAINER` env. |
| 359 | `test_is_docker_environment_hostname` | **Strong** | `is True` via hostname substring match. |
| 368 | `test_is_docker_environment_not_docker` | **Strong** | `is False` when none of the signals fire. Negative cell of the matrix. |

## TestSetupWorkingDirectory

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 380 | `test_setup_working_directory` | **Strong** | 4 assertions: exists, is dir, has tmpdir prefix, contains `plex_previews_` token. |
| 392 | `test_setup_working_directory_unique` | **Strong** | Pins uniqueness across calls AND existence of both. |
| 403 | `test_setup_working_directory_creates_if_missing` | **Strong** | Pins recursive creation when base doesn't exist. |

## TestAtomicJsonSaveWithBackup

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 428 | `test_first_write_no_bak` | **Strong** | Equality `== []` for backup count + existence check on target. |
| 437 | `test_subsequent_write_creates_timestamped_bak_with_old_contents` | **Strong** | Pins filename shape (15 chars + `-` at idx 8), backup count == 1, AND old-contents preservation (`v == 1` in bak, `v == 2` in target). |
| 456 | `test_keeps_history_across_many_writes` | **Strong** | Strict count `== 4` after 5 writes. Catches old rolling-single regression. |
| 472 | `test_prunes_oldest_beyond_retention` | **Strong** | Strict count `== 3` with `CONFIG_BACKUP_KEEP=3`. |
| 486 | `test_legacy_single_bak_is_not_disturbed` | **Strong** | Existence + content equality on legacy file. |
| 501 | `test_prune_drops_anything_older_than_max_age_days` | **Strong** | Two assertions: old is gone, recent survives — pins age-based AND independent-of-count behavior. |
| 525 | `test_prune_max_age_zero_disables_age_check` | **Strong** | Pins zero-disables-age contract for backwards compat. |
| 543 | `test_backup_failure_does_not_block_primary_write` | **Strong** | Pins best-effort backup contract via real OSError injection + content equality on the primary write. |

## Summary

- **36 tests** — 31 Strong, 5 Weak

**File verdict: MIXED → mostly STRONG.**

### Weak tests to fix:
- **L29** `test_calculate_title_width` — only range bounds. Add equality pin against the formula's actual output for cols=120.
- **L41** `test_calculate_title_width_small_terminal` — `>= 20` only. Pin actual value or assert it equals the floor sentinel.
- **L53** `test_calculate_title_width_large_terminal` — `<= 50` only. Same fix as above.
- **L91** `test_format_display_title_movie` — OR clause `"Shawshank" in result or title in result` accepts almost anything for the title check; add strict equality.
- **L230** `test_exact_root_match_allowed` — only `is not None`. Should `assert result == str(tmp_path.resolve())`.

### Notes:
- TestSafeResolveWithin is overall an exemplary security-test class with documented threat models per test.
- Backup tests cover D17 timestamped-backup contract robustly.
